from nmigen import *


class IntegerUnit(Elaboratable):
    def __init__(self, registerFile):

        self.registerFile = registerFile

        # inputs from ROB
        self.operation = Signal(5)
        self.regA = Signal(registerFile.addr_width)
        self.regB = Signal(registerFile.addr_width)
        self.regOut = Signal(registerFile.addr_width)
        self.imm  = Signal(16)

        # outputs
        self.stalled = Signal()
