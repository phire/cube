from nmigen import *

# Each decoder decodes one instruction and outputs ONE uop into the ROB.
#   TODO: We will eventually need a slow path that inserts multiple uops from a microcode rom
class Decoder(Elaboratable):

    def __init__(self):
        self.inst = Signal(32)
        self.executionUnits = Signal(6)
        self.opcode = Signal(6)
        self.regA = Signal(6)
        self.regB = Signal(6)
        self.regOut = Signal(6)
        self.immediate = Signal(16)
        self.valid = Signal(3) # one for each reg

        self.dummy_icache = Memory(width=5*3, depth=256, init=[
            0b00001_00010_00001,
            0b00001_00010_00001,
            0b00001_00010_00001,
            0b00001_00010_00001,
            0b00001_00010_00001,
            0b00001_00010_00001,
            0b00001_00010_00001,
            0b00001_00010_00001,
            0b00001_00010_00011,
            0b00001_00010_00011,
            0b00011_00010_00101,
            0b00011_00000_01101,
            0b00011_00000_01101,
            0b00011_10000_01101,
            0b01001_01000_01101,
            0b00101_00100_01101,
            0b10001_00110_01101,
        ] * 15)

    def elaborate(self, platform):
        m = Module()

        # Takes in 32bit opcode

        counter = Signal(8)

        m.d.sync += [
            # Just continually output dummy instructions from icache
            self.executionUnits.eq(Const(7)), # Can execute on execution units 1, 2 or 3
            self.opcode.eq(Const(13)), # random opcode. Might mean add
            self.regA.eq(self.dummy_icache[counter][10:14]),
            self.regB.eq(self.dummy_icache[counter][5:9]),
            self.regOut.eq(self.dummy_icache[counter][0:4]),
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