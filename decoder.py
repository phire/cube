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

    def elaborate(self, platform):
        m = Module()

        # Takes in 32bit opcode

        m.d.sync += [
            # Just continually output a dummy instruction
            # This is something like add r1, r1, r2
            self.executionUnits.eq(Const(7)), # Can execute on execution units 1, 2 or 3
            self.opcode.eq(Const(13)), # random opcode. Might mean add
            self.regA.eq(Const(1)),
            self.regB.eq(Const(2)),
            self.regOut.eq(Const(1)),
            self.immediate.eq(Const(0)), # unused
            self.valid.eq(Const(7))
        ]

        # Outputs:
        #   * Which execution unit(s) can execute this
        #   * With what opcode
        #   * Which arch register reads
        #   * Which arch register write
        #   * Immediate
        #   * If uop might cause a rollback

        return m