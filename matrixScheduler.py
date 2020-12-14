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
        m.d.comb += self.all_clear.eq(self.values == 0)

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

    def __init__(self, size, num_sets):
        self.size = size
        self.num_sets = num_sets
        addr_width = (size-1).bit_length()

        # set ports
        self.row_selects = [Signal(size, name=f"row_selects_{i}") for i in range(num_sets)]
        self.row_data = [Signal(size, name=f"row_data_{i}") for i in range(num_sets)]

        # clear ports
        self.clear_hot = Signal(size, name=f"clear_data")

        # outputs
        self.is_clear = Signal(size)

        self.rows = [MatrixRow(size, num_sets, i) for i in range(1, size)]

    def elaborate(self, platform):
        m = Module()

        for i, row in enumerate(self.rows):
            m.submodules[f"row_{row.id}"] = row

        for row in self.rows:
            # Hookup each row's row_selects
            for row_select, all_selects in zip(row.row_selects, self.row_selects):
                m.d.comb += row_select.eq(all_selects[row.id])

            # Distribute the row_sets for each set port
            for row_set, src_set in zip(row.row_sets, self.row_data):
                m.d.comb += row_set.eq(Cat(*src_set))

            # Distribute the clear signals
            m.d.comb += row.clears.eq(self.clear_hot)

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

        self.outHot = [Signal(size, name=f"Selected_1hot_{i}") for i in range(num_outs)]

    def elaborate(self, platform):
        m = Module()

        negated = Signal(self.size)

        # FPGAs have dedicated carry propagation chains in their adders which we can take
        # advantage of to quickly find the first bit
        m.d.comb += negated.eq(~self.input + 1)

        prevInput = self.input
        prevNegated = negated

        for outHot in self.outHot:
            nextInput = Signal(prevInput.width - 1)
            nextNegated = Signal(prevNegated.width - 1)

            m.d.comb += [
                outHot.eq(prevNegated & prevInput),
                nextNegated.eq((prevNegated + outHot) >> 2),
                nextInput.eq(prevInput >> 1)
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

        self.clear_addr = [Signal(width) for i in range(Impl.NumDecodes)]

        # wakeup matrix
        self.matrix = Matrix(Impl.numRenamingRegisters, Impl.NumDecodes)

        self.selecter = PiorityEncoder(self.matrix.size, self.NumIssues)

        # Tracks which instructions in the matrix would be elegable for select if all their dependences are met
        self.waiting_for_select = Signal(self.NumQueueEntries)


        # Outputs

        # TODO: Should the number of readies be equal to decode width?
        self.ready = [Signal(width, name=f"ready{i}") for i in range(Impl.NumDecodes)]
        self.readyHot = [Signal(self.NumQueueEntries, name=f"ready_hot{i}") for i in range(Impl.NumDecodes)]
        self.readyValid = [Signal(width, name=f"ready{i}_valid") for i in range(Impl.NumDecodes)]


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

            with m.If(self.inValid[i]):
                m.d.comb += self.matrix.row_selects[i].eq(selectDecoder.o)
            with m.Else():
                m.d.comb += self.matrix.row_selects[i].eq(0)

            all_row_selects += [self.matrix.row_selects[i]]


        Clears = []
        for i in range(self.NumIssues):
            m.submodules[f"clear_decoder_{i}"] = clearDecoder = Decoder(self.NumQueueEntries)

            m.d.comb += clearDecoder.i.eq(self.clear_addr[i])
            Clears += [clearDecoder.o]
        m.d.comb += self.matrix.clear_hot.eq(acclumnateOR(m.d.comb, Clears))


        # The selector takes the output of the matrix and chooses NumIssue instructions that are ready
        m.submodules += self.selecter

        m.d.comb += self.selecter.input.eq(self.matrix.is_clear & (self.waiting_for_select)),

        for outHot, readyHot, readyValid in zip(self.selecter.outHot, self.readyHot, self.readyValid):
            m.d.comb += [
                readyHot.eq(outHot),
                readyValid.eq(outHot[1:] != 0)
            ]

        InsertedThisCycle = acclumnateOR(m.d.comb, all_row_selects)
        SelectedThisCycle = acclumnateOR(m.d.comb, self.selecter.outHot)

        # We want to remove the uops we selected this cycle from the eligible set
        # And mark any new uops as eligible
        m.d.sync += self.waiting_for_select.eq((self.waiting_for_select & ~SelectedThisCycle) | InsertedThisCycle)

        return m



if __name__ == "__main__":
    from nmigen.back.pysim import *


    class Impl:
        NumDecodes = 4
        NumIssues = 4
        numRenamingRegisters = 64

    scheduler = MatrixScheduler(Impl(), None)

    code = [
        (1, 0, 0),
        (2, 1, 1),
        (3, 2, 2),
        (4, 3, 3),
        (5, 4, 0),
        (6, 5, 0),
        (7, 6, 0),
        (8, 7, 7),
        (9, 8, 8),
        (10, 9, 9),
        (11, 10, 10),
        (12, 11, 11),
        (13, 12, 12),
        (14, 13, 13),
        (15, 14, 14),
        (16, 15, 15),
    ]
    counter = 0

    old_matrix = [0] * Impl().numRenamingRegisters

    def printState(scheduler):
        print("-- Cycle --")

        for row in scheduler.matrix.rows:
            current_value = (yield row.values)
            if old_matrix[row.id] != current_value:
                print(f"row{row.id} = {bin(current_value)}")
                old_matrix[row.id] = current_value

        waiting_for_select = (yield scheduler.waiting_for_select)
        print(bin(waiting_for_select))
        print(bin((yield scheduler.selecter.input)))

        for i in range(len(scheduler.inA)):
            global counter
            if counter < len(code):
                (out, A, B) = code[counter]
                print(f"  scheduling {out}: {A}, {B}")
                yield scheduler.inOut[i].eq(out)
                yield scheduler.inA[i].eq(A)
                yield scheduler.inB[i].eq(B)
                yield scheduler.inValid[i].eq(True)
                counter += 1
            else:
                yield scheduler.inValid[i].eq(False)

        for i in range(len(scheduler.ready)):
            readyValid = (yield scheduler.readyValid[i])
            readyHot = (yield scheduler.readyHot[i])
            if readyValid:
                print(f"\t{constEncode(readyHot)} is ready")
                if(constEncode(readyHot) == 0):
                    print(hex(readyHot))
                yield scheduler.clear_addr[i].eq(constEncode(readyHot))
            else:
                yield scheduler.clear_addr[i].eq(0)


    with Simulator(scheduler) as sim:
        def process():

            for i in range(len(scheduler.inValid)):
                yield scheduler.inValid[i].eq(False)

            for _ in range(20):
                yield from printState(scheduler)
                yield Tick()
        sim.add_clock(0.0001)
        sim.add_process(process)
        sim.run()

    ports = [scheduler.ready[0], scheduler.readyValid[0]]

    for inOut, inA, inB, inValid in zip(scheduler.inOut, scheduler.inA, scheduler.inB, scheduler.inValid):
        ports += [inOut, inA, inB, inValid]

    main(scheduler, ports = ports)