from nmigen import *
from nmigen.lib.coding import *
from nmigen.cli import main
from multiMem import MultiMem
from util import *

class MatrixRow(Elaboratable):
    def __init__(self, num_values, num_sets, row_id):
        self.num_values = num_values
        self.num_sets = num_sets
        self.id = row_id

        # inputs
        self.row_selects  = [Signal(name=f"row_select_{i}") for i in range(num_sets)]
        self.row_sets = [Signal(num_values) for j in range(num_sets)]

        self.clears = Signal(num_values, name=f"col_clear")

        # State
        self.values = Signal(num_values, name=f"row_{row_id}_values") # Value of each matrix cell

        # Constants
        self.permanent_mask = Const(~(1 | 1 << row_id))

        # outputs
        self.all_clear = Signal(name=f"row_{row_id}_all_clear")

    def elaborate(self, platform):
        m = Module()

        # Mask and combine all row selects so we have a single row_set that's only
        # relevant  to this row
        this_row_set = Const(0)
        for i in range(self.num_sets):
            masked_row_set = Signal(self.num_values, name=f"select_{i}_masked")
            this_row_set_acc = Signal(self.num_values, name=f"this_row_set_acc_{i}")

            m.d.comb += [
                masked_row_set.eq(Mux(self.row_selects[i], self.row_sets[i], 0)),
                this_row_set_acc.eq(this_row_set | masked_row_set)
            ]

            this_row_set = this_row_set_acc

        # Update all cells in the row
        m.d.sync += self.values.eq(((this_row_set | self.values) & ~self.clears) & self.permanent_mask)

        # Check if whole row is clear
        m.d.sync += self.all_clear.eq(self.values == 0)

        return m

class Matrix(Elaboratable):
    # Future optimization idea:
    #   If we arrange the allocation IDs so they are always allocated to instructions in pairs
    #   Then we can wire the odd set ports to odd rows and even set ports to even rows.
    #
    #   Testing shows this will roughly cut the ALM count of this matrix in half
    #   which might be desirable because it is quite large.
    #   The main downside to this optimization is if we only need half of a pair in one cycle
    #   the other half is unusable until both are freed

    def __init__(self, size, num_sets, num_sets_per_row, num_clears):
        self.size = size
        self.num_sets = num_sets
        addr_width = (size-1).bit_length()

        # set ports
        self.row_selects = [Signal(size, name=f"row_selects_{i}") for i in range(num_sets)]
        self.row_data = [Signal(size, name=f"row_data_{i}") for i in range(num_sets)]

        # clear ports
        self.clear_col_addr = Array(Signal(addr_width, name=f"clear_addr_{i}") for i in range(num_clears))

        # outputs
        self.is_clear = Signal(size)

        self.rows = [MatrixRow(size, num_sets, i) for i in range(1, size)]

    def elaborate(self, platform):
        m = Module()

        for i, row in enumerate(self.rows):
            m.submodules[f"row_{row.id}"] = row

        col_clears = Array(Signal() for _ in range(self.size))
        m.d.comb += Cat(*col_clears).eq(0)

        # clear column address decoders
        for clear_port in self.clear_col_addr:
            col_clears[clear_port].eq(1)


        for row in self.rows:
            # Hookup each row's row_selects
            for row_select, all_selects in zip(row.row_selects, self.row_selects):
                m.d.comb += row_select.eq(all_selects[row.id])

            # Distribute the row_sets for each set port
            for row_set, src_set in zip(row.row_sets, self.row_data):
                m.d.comb += row_set.eq(Cat(*src_set))

            # Distribute the clear signals
            m.d.comb += row.clears.eq(Cat(col_clears))

            # Collect all_clear signals
            m.d.comb += self.is_clear[row.id].eq(row.all_clear)

        # row zero is hardwired to clear
        m.d.comb += self.is_clear[0].eq(1)

        return m

class PiorityEncoder(Elaboratable):
    # Takes N inputs
    # Returns the encoded ID of the first M high input

    # Future optimization ideas:
    #  * For our usecase, we don't really need priority. Any M outputs will do
    #    We can cut the depth in half by having two half-sized priority encoders
    #    working independently in opposite directions. Some simple post-filtering
    #    can remove duplicate results that arise when less than M inputs are hot.
    #    We don't care about duplicates in this cycle, so  duplicate detection can
    #    be done in a future pipeline stage if needed
    #  * We might not care if the detection is lossy.
    #    By splitting this into two banks, the carry chain can be cut in half
    #    The downside is that if the two banks aren't balanced, half out outputs
    #    might be incorrectly NULL

    def __init__(self, size, num_outs):
        self.size = size
        self.width = width = (size-1).bit_length()
        self.num_outs = num_outs

        self.input = Signal(size)

        self.out = Array(Signal(width, name=f"Selected_{i}") for i in range(num_outs))
        self.outHot = [Signal(size, name=f"Selected_1hot_{i}") for i in range(num_outs)]

    def elaborate(self, platform):
        m = Module()

        negated = Signal(self.size)

        # FPGAs have dedicated carry propagation chains in their adders which we can take
        # advantage of to quickly find the first bit
        m.d.comb += negated.eq(~self.input + 1)

        prevInput = self.input
        prevNegated = negated

        for out, outHot in zip(self.out, self.outHot):
            outRegisterd = Signal(self.size)
            nextInput = Signal(self.size)
            nextNegated = Signal(self.size)

            # Note Encoding a 64bit value is really expensive
            encoder = Encoder(self.size)

            m.submodules += encoder

            # Copy the one-hot so the encoding doesn't mess with our timing measurements
            m.d.sync += outRegisterd.eq(outHot)

            m.d.comb += [
                outHot.eq(prevNegated & prevInput),
                nextNegated.eq(prevNegated >> 1),
                nextInput.eq((nextNegated + outHot) >> 1),

                # Keep the old api by still outputting encoded outputs
                encoder.i.eq(outRegisterd),
                out.eq(Mux(encoder.n, Const(0), encoder.o)),
            ]
            (prevInput, prevNegated) = (nextInput, nextNegated)

        return m


class MatrixScheduler(Elaboratable):
    # Takes the output from renamer, stores it in a queue until all dependencies are met
    # and pushs them out to a ready queue

    # TODO: The current implementation uses the renamingID directly as the queueID
    #       This is simple, but wastes a bunch of space in scheduler/rob structures for the renaming
    #       registers backing arch registers of completed instructions

    def __init__(self, Impl, Arch):
        self.width = width = Impl.numRenamingRegisters.bit_length()
        self.NumIssues = Impl.NumIssues
        self.NumDecodes = Impl.NumDecodes
        self.NumQueueEntries = Impl.numRenamingRegisters

        # Inputs from renamer

        self.inA = [Signal(width, name=f"inA_{i}") for i in range(Impl.NumDecodes)]
        self.inB = [Signal(width, name=f"inB_{i}") for i in range(Impl.NumDecodes)]
        self.inOut = [Signal(width, name=f"inOut_{i}") for i in range(Impl.NumDecodes)]
        self.inValid = [Signal(name=f"inValid_{i}") for i in range(Impl.NumDecodes)]

        self.clear_addr = Signal(width)

        # wakeup matrix
        self.matrix = Matrix(Impl.numRenamingRegisters, Impl.NumDecodes, 2, Impl.NumIssues)

        # Tracks which instructions in the matrix would be elegable for select if all their dependences are met
        self.waiting_for_select = Signal(Impl.numRenamingRegisters)



        # Outputs

        # TODO: Should the number of readies be equal to decode width?
        self.ready = Array(Signal(width, name=f"ready{i}") for i in range(Impl.NumDecodes))
        self.readyValid = Array(Signal(width, name=f"ready{i}_valid") for i in range(Impl.NumDecodes))


    def elaborate(self, platform):
        m = Module()

        m.submodules["bit_matrix"] = self.matrix
        all_row_selects = []

        # Decode inputs to 1-hot and pass into the matrix
        for i in range(self.NumDecodes):
            m.submodules[f"select_decoder_{i}"] = selectDecoder = Decoder(self.NumQueueEntries)
            m.submodules[f"argA_decoder_{i}"] = argADecoder = Decoder(self.NumQueueEntries)
            m.submodules[f"argB_decoder_{i}"] = argBDecoder = Decoder(self.NumQueueEntries)

            m.d.comb += [
                selectDecoder.i.eq(self.inOut[i]),
                argADecoder.i.eq(self.inA[i]),
                argBDecoder.i.eq(self.inB[i]),

                self.matrix.row_data[i].eq(argADecoder.o | argBDecoder.o),
            ]

            all_row_selects += [selectDecoder.o]

            with m.If(self.inValid[i]):
                m.d.comb += self.matrix.row_selects[i].eq(selectDecoder.o)
            with m.Else():
                m.d.comb += self.matrix.row_selects[i].eq(0)

        # Dummy code to prevent the currently unused clear logic from being optimized away
        m.d.comb += self.matrix.clear_col_addr[0].eq(self.clear_addr)
        m.d.comb += self.matrix.clear_col_addr[1].eq(self.clear_addr ^ 0b010101)
        m.d.comb += self.matrix.clear_col_addr[2].eq(self.clear_addr + 5)
        m.d.comb += self.matrix.clear_col_addr[3].eq(self.clear_addr + 16)


        # The selector takes the output of the matrix and chooses NumIssue instructions that are ready
        m.submodules["selector"] = selecter = PiorityEncoder(self.matrix.size, self.NumIssues)

        m.d.comb += selecter.input.eq(self.matrix.is_clear & self.waiting_for_select),

        for encoder_out, ready, readyValid in zip(selecter.out, self.ready, self.readyValid):
            m.d.comb += [
                ready.eq(encoder_out),
                readyValid.eq(encoder_out != 0)
            ]

        InsertedThisCycle = acclumnateOR(m.d.comb, all_row_selects)
        SelectedThisCycle = acclumnateOR(m.d.comb, selecter.outHot)

        # We want to remove the uops we selected this cycle from the eligible set
        # And mark any new uops as eligible
        m.d.sync += self.waiting_for_select.eq((self.waiting_for_select & ~SelectedThisCycle) | InsertedThisCycle)

        return m

if __name__ == "__main__":

    class Impl:
        NumDecodes = 4
        NumIssues = 4
        numRenamingRegisters = 64

    mm = MatrixScheduler(Impl(), None)

    ports = [mm.ready[0], mm.readyValid[0]]

    for inOut, inA, inB, inValid in zip(mm.inOut, mm.inA, mm.inB, mm.inValid):
        ports += [inOut, inA, inB, inValid]

    main(mm, ports = ports)