from nmigen import *
from nmigen.cli import main
from multiMem import MultiMem
from renamer import Renamer


class Matrixvalue(Elaboratable):

    def __init__(self, addr_width, num_sets, id):
        self.num_sets = num_sets
        self.id = id

        self.value = Signal()
        self.addr = Array(Signal(addr_width, name=f"addr_{id}_{i}") for i in range(num_sets))
        self.row_set  = Array(Signal(name=f"row_set_{id}_{i}") for i in range(num_sets))
        self.col_clear = Signal()

    def elaborate(self, platform):
        m = Module()

        selected = Const(0)

        for row_set, set_addr in zip(self.row_set, self.addr):
            new_selected = Signal()
            m.d.comb += new_selected.eq(selected | (row_set & (set_addr == Const(self.id))))
            selected = new_selected


        with m.If(self.col_clear): # A clear overridess sets
            m.d.sync += self.value.eq(False)
        with m.Elif(selected):
            m.d.sync += self.value.eq(True)
        with m.Else():
            m.d.sync += self.value.eq(self.value)

        return m

class MatrixRow(Elaboratable):
    def __init__(self, num_values, num_sets, row_id):
        self.num_sets = num_sets
        self.id = row_id
        addr_width = (num_values-1).bit_length()

        self.addr = Array(Signal(addr_width) for _ in range(num_sets))
        self.row_set  = Array(Signal() for _ in range(num_sets))

        self.clears = Array(Signal() for _ in range(num_values))

        self.values = [Matrixvalue(addr_width, num_sets, i) for i in range(1, num_values) if id != row_id]

        # outputs
        self.all_clear = Signal()

    def elaborate(self, platform):
        m = Module()

        all_clear = Const(1)

        for value in self.values:
            m.submodules += value

            # Hook up all set ports
            for i in range(self.num_sets):
                m.d.comb += [
                    value.addr[i].eq(self.addr[i]),
                    value.row_set[i].eq(self.row_set[i])
                ]

            # Hook up the column clear signal
            m.d.comb += value.col_clear.eq(self.clears[i])

            # determine if all values in the row are clear
            # TODO: Maybe we should delay this by a cycle?
            is_clear = Signal(name=f"value_{value.id}_is_clear")
            all_clear_acc = Signal(name=f"value_{value.id}_accumulator")
            m.d.comb += [
                is_clear.eq(value.value == 0),
                all_clear_acc.eq(is_clear & all_clear)
            ]
            all_clear = all_clear_acc

        m.d.comb += self.all_clear.eq(all_clear)
        return m

class Matrix(Elaboratable):

    def __init__(self, size, num_sets, num_sets_per_row, num_clears):
        self.size = size
        addr_width = (size-1).bit_length()

        # set ports
        self.row_addr = Array(Signal(addr_width) for _ in range(num_sets))
        self.col_addr = Array(Array(Signal(addr_width) for _ in range(num_sets_per_row)) for _ in range(num_sets))

        # clear ports
        self.clear_col_addr = Array(Signal(addr_width) for _ in range(num_clears))

        # outputs
        self.is_clear = Signal(size)

        self.rows = Array(MatrixRow(size, num_sets, i) for i in range(1, size))

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
                    m.d.comb += row.addr[j].eq(col_addr)

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
        numRenamingRegisters = 32

    mm = MatrixScheduler(Impl(), None)

    ports = [mm.ready[0], mm.readyValid[0]]

    for inOut, inA, inB, inValid in zip(mm.inOut, mm.inA, mm.inB, mm.inValid):
        ports += [inOut, inA, inB, inValid]

    # for col_addrs in mm.col_addr:
    #     for col_addr in col_addrs:
    #         ports += [col_addr]

    # for clear_col_addr in mm.clear_col_addr:
    #     ports += [clear_col_addr]

    # for is_clear in mm.is_clear:
    #     ports += [is_clear]

    main(mm, ports = ports)