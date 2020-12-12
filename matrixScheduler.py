from nmigen import *
from nmigen.cli import main
from multiMem import MultiMem
from renamer import Renamer

class MatrixRow(Elaboratable):
    def __init__(self, num_values, num_sets, row_id):
        self.num_values = num_values
        self.num_sets = num_sets
        self.id = row_id

        self.row_selects  = [Signal(name=f"row_select_{i}") for i in range(num_sets)]
        self.row_sets = [Signal(num_values) for j in range(num_sets)]

        self.clears = Signal(num_values, name=f"col_clear")

        self.values = Signal(num_values, name=f"row_{row_id}_values")

        self.permanent_mask = Const(~(1 | 1 << row_id))

        # outputs
        self.all_clear = Signal()

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
        self.row_addr = [Signal(addr_width, name=f"row_addr_{i}") for i in range(num_sets)]
        self.col_addr = [[Signal(addr_width, name=f"col_addr_{i}_{j}") for j in range(num_sets_per_row)] for i in range(num_sets)]

        # clear ports
        self.clear_col_addr = Array(Signal(addr_width, name=f"clear_addr_{i}") for i in range(num_clears))

        # outputs
        self.is_clear = Signal(size)

        self.rows = Array(MatrixRow(size, num_sets, i) for i in range(1, size))

    def elaborate(self, platform):
        m = Module()

        m.submodules += self.rows

        col_clears = Array(Signal() for _ in range(self.size))
        m.d.comb += Cat(*col_clears).eq(0)

        # clear column address decoders
        for clear_port in self.clear_col_addr:
            col_clears[clear_port].eq(1)

        # Precalculate row selects and
        all_row_selects = [Array(Signal() for _ in range(self.size)) for i in range(self.num_sets)]
        row_sets = [Array(Signal() for _ in range(self.size)) for i in range(self.num_sets)]

        for i, (select, row_set) in enumerate(zip(all_row_selects, row_sets)):
            m.d.comb += [
                Cat(*select).eq(0), # clear
                Cat(*row_set).eq(0),
                select[self.row_addr[i]].eq(1),
            ]

            for col_addr in self.col_addr[i]:
                m.d.comb += row_set[col_addr].eq(1)

        for row in self.rows:
            # Hookup each row's row_selects
            for row_select, all_selects in zip(row.row_selects, all_row_selects):
                m.d.comb += row_select.eq(all_selects[row.id])

            # Distribute the row_sets for each set port
            for row_set, src_set in zip(row.row_sets, row_sets):
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

    def __init__(self, size, num_outs):
        self.width = width = (size-1).bit_length()
        self.num_outs = num_outs

        self.input = Signal(size)

        self.out = Array(Signal(width, name=f"Selected_{i}") for i in range(num_outs))

    def elaborate(self, platform):
        m = Module()

        for out in self.out:
            m.d.comb += out.eq(0)

        # Maybe there is a smarter (or less perfect) way to do this
        # But we just brute force it
        count_prev = Const(0)
        for i in range(len(self.input)):
            if i > 0:
                for j in range(self.num_outs):
                    count = Signal((self.num_outs + 1).bit_length())

                    with m.If((self.input[i] == True) & (count_prev < self.num_outs)):
                        m.d.comb += [
                            self.out[count_prev].eq(i),
                            count.eq(count_prev + 1)
                        ]
                    with m.Else():
                        m.d.comb += count.eq(count_prev)

                    count_prev = count

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

        # Inputs from renamer

        self.inA = [Signal(width, name=f"inA_{i}") for i in range(Impl.NumDecodes)]
        self.inB = [Signal(width, name=f"inB_{i}") for i in range(Impl.NumDecodes)]
        self.inOut = [Signal(width, name=f"inOut_{i}") for i in range(Impl.NumDecodes)]
        self.inValid = [Signal(name=f"inValid_{i}") for i in range(Impl.NumDecodes)]

        self.clear_addr = Signal(width)

        # wakeup matrix
        self.matrix = Matrix(Impl.numRenamingRegisters, Impl.NumDecodes, 2, Impl.NumIssues)

        # Outputs

        # TODO: Should the number of readyies be equal to decode width
        #self.ready = [Signal(width, name=f"ready{i}") for i in range(self.NumWakeupChecks)]
        #self.readyValid = [Signal(name=f"ready{i}_valid") for i in range(self.NumWakeupChecks)]

        self.ready = Array(Signal(width) for i in range(Impl.NumDecodes))
        self.readyValid = Array(Signal(width) for i in range(Impl.NumDecodes))



    def elaborate(self, platform):
        m = Module()

        m.submodules += self.matrix

        for i in range(self.NumDecodes):
            with m.If(self.inValid[i]):
                m.d.comb += [
                    self.matrix.row_addr[i].eq(self.inOut[i]),
                    self.matrix.col_addr[i][0].eq(self.inA[i]),
                    self.matrix.col_addr[i][1].eq(self.inB[i]),
                ]
            with m.Else():
                m.d.comb += self.matrix.row_addr[i].eq(0)

        m.d.comb += self.matrix.clear_col_addr[0].eq(self.clear_addr)
        m.d.comb += self.matrix.clear_col_addr[1].eq(self.clear_addr ^ 0b010101)
        m.d.comb += self.matrix.clear_col_addr[2].eq(self.clear_addr + 5)
        m.d.comb += self.matrix.clear_col_addr[3].eq(self.clear_addr + 16)

        encoder = PiorityEncoder(self.matrix.size, self.NumIssues)
        m.submodules += encoder

        m.d.comb += encoder.input.eq(self.matrix.is_clear),

        for encoder_out, ready, readyValid in zip(encoder.out, self.ready, self.readyValid):
            m.d.comb += [
                ready.eq(encoder_out),
                readyValid.eq(encoder_out != 0)
            ]

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