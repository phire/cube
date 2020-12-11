from nmigen import *

# Each decoder decodes one instruction and outputs ONE uop into the ROB.
#   TODO: We will eventually need a slow path that inserts multiple uops from a microcode rom
class Decoder(Elaboratable):

    def __init__(self, offset):
        self.offset = offset
        self.inst = Signal(32)
        self.executionUnits = Signal(6)
        self.opcode = Signal(6)
        self.regA = Signal(6)
        self.regB = Signal(6)
        self.regOut = Signal(6)
        self.immediate = Signal(16)
        self.valid = Signal(3) # one for each reg

        self.dummy_icache = Memory(width=5*3, depth=256, init=[
            0b000001_000010_000001,
            0b000001_100010_100001,
            0b000001_000010_000001,
            0b000001_100010_100001,
            0b000001_000010_000001,
            0b000001_100010_100001,
            0b000001_000010_000001,
            0b000001_100010_100001,
            0b000001_000010_000011,
            0b100001_100010_100011,
            0b100011_000010_000101,
            0b100011_100000_101101,
            0b100011_000000_001101,
            0b100011_110000_101101,
            0b101001_001000_001101,
            0b100101_100100_101101,
            0b110001_000110_001101,
        ] * 15)

    def elaborate(self, platform):
        m = Module()

        # Takes in 32bit opcode

        counter = Signal(8, reset=self.offset)

        m.d.sync += [
            # Just continually output dummy instructions from icache
            self.executionUnits.eq(Const(7)), # Can execute on execution units 1, 2 or 3
            self.opcode.eq(Const(13)), # random opcode. Might mean add
            self.regA.eq(self.dummy_icache[counter][12:17]),
            self.regB.eq(self.dummy_icache[counter][6:11]),
            self.regOut.eq(self.dummy_icache[counter][0:5]),
            self.immediate.eq(Const(0)), # unused
            self.valid.eq(Const(7)),

            counter.eq(counter + 1)
        ]

        # Outputs:
        #   * Which execution unit(s) can execute this
        #   * With what opcode
        #   * Which arch register reads
        #   * Which arch register write
        #   * Immediate
        #   * If uop might cause a rollback

        return m