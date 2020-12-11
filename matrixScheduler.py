from nmigen import *
from nmigen.cli import main
from multiMem import MultiMem
from renamer import Renamer

class MatrixRow(Elaboratable):
    def __init__(self, num_values, num_sets, num_sets_per_row, row_id):
        self.num_values = num_values
        self.num_sets = num_sets
        self.id = row_id
        addr_width = (num_values-1).bit_length()

        self.addr = Array(Array(Signal(addr_width, name=f"row_addr_{i}_{j}") for j in range(num_sets_per_row)) for i in range(num_sets))
        self.row_set  = Array(Signal(name=f"row_set_{i}") for i in range(num_sets))

        self.clears = Signal(num_values, name=f"col_clear")

        self.values = Signal(num_values, name=f"row_{row_id}_values")

        # outputs
        self.all_clear = Signal()

    def elaborate(self, platform):
        m = Module()

        for i in range(self.num_values):
            if i == self.id or i == 0: # skip row == col and the zero column
                continue

            col_clear = Signal()
            m.d.comb += col_clear.eq(self.clears[i])

            selected = Const(0)

            for row_set, addrs in zip(self.row_set, self.addr):
                for addr in addrs:
                    new_selected = Signal(name=f"row_{self.id}_col_{i}_selected")
                    m.d.comb += new_selected.eq(selected | (row_set & (addr == Const(self.id))))
                    selected = new_selected

            with m.If(col_clear): # A clear overrides sets
                m.d.sync += self.values[i].eq(False)
            with m.Elif(selected):
                m.d.sync += self.values[i].eq(True)
            with m.Else():
                m.d.sync += self.values[i].eq(self.values[i])


        m.d.comb += self.all_clear.eq(self.values == 0)
        return m

class Matrix(Elaboratable):

    def __init__(self, size, num_sets, num_sets_per_row, num_clears):
        self.size = size
        addr_width = (size-1).bit_length()

        # set ports
        self.row_addr = Array(Signal(addr_width, name=f"row_addr_{i}") for i in range(num_sets))
        self.col_addr = Array(Array(Signal(addr_width, name=f"col_addr_{i}_{j}") for j in range(num_sets_per_row)) for i in range(num_sets))

        # clear ports
        self.clear_col_addr = Array(Signal(addr_width, name=f"clear_addr_{i}") for i in range(num_clears))

        # outputs
        self.is_clear = Signal(size)

        self.rows = Array(MatrixRow(size, num_sets, num_sets_per_row, i) for i in range(1, size))

    def elaborate(self, platform):
        m = Module()

        m.submodules += self.rows

        # clear column address decoders
        for i in range(self.size):
            col_clear = Const(0)

            # Do any of the clear ports have the address of this column?
            for col_addr in self.clear_col_addr:
                new_col_clear = Signal()
                m.d.comb += new_col_clear.eq(col_addr == Const(i) | col_clear)
                col_clear = new_col_clear

            # Distibute the clear signal to all rows
            for row in self.rows:
                m.d.comb += row.clears[i].eq(col_clear)

        # row address decoders
        for row in self.rows:
            for j, (row_addr, col_addrs) in enumerate(zip(self.row_addr, self.col_addr)):
                m.d.comb += row.row_set[j].eq(row_addr == row.id)
                for k, col_addr in enumerate(col_addrs):
                    m.d.comb += row.addr[j][k].eq(col_addr)

            m.d.comb += self.is_clear[row.id].eq(row.all_clear)

        # row zero is hardwired to clear
        m.d.comb += self.is_clear[0].eq(1)

        return m

class PiorityEncoder(Elaboratable):
    def __init__(self, size):
        width = (size-1).bit_length()

        self.input = Signal(size)

        self.out = Signal(width)

    def elaborate(self, platform):
        m = Module()

        for i in range(len(self.input)):
            if i >= 0:
                with m.If(self.input[i] == True):
                    m.d.comb += self.out.eq(i)

        return m


class MatrixScheduler(Elaboratable):
    # Takes the output from renamer, stores it in a queue until all dependencies are met
    # and pushs them out to a ready queue

    # TODO: The current implementation uses the renamingID diredtly as the queueID
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

        self.ready = [Signal(width)]
        self.readyValid = [Signal()]



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

        # This only gets us one ready per cycle
        # # Which is not enough :P
        encoder = PiorityEncoder(self.matrix.size)
        m.submodules += encoder

        m.d.comb += [
            encoder.input.eq(self.matrix.is_clear),
            self.ready[0].eq(encoder.out),
            self.readyValid[0].eq(encoder.out != 0)
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