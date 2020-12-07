from nmigen import *
from nmigen.cli import main
from decoder import Decoder
from renamer import Renamer
from scheduler import Scheduler

from nmigen_boards.de10_nano import *

class Arch:
    NumGPR = 32

class Impl:
    NumDecodes = 4
    NumIssues = 4
    numFinalizes = 4
    numExecutions = 4
    numEntries = 128
    numRenamingRegisters = 64
    MWTSize = 16

class Pipeline(Elaboratable):

    def __init__(self, Impl, Arch):

        self.renamer = Renamer(Impl, Arch)
        self.scheduler = Scheduler(Impl, Arch, self.renamer)


    def elaborate(self, platform: DE10NanoPlatform):
        m = Module()

        m.submodules.renamer = self.renamer
        m.submodules.scheduler = self.scheduler


        led = [platform.request("led", i) for i in range(8)]
        led_buffer = Signal(8)

        switch = platform.request("switch", 0)
        switch_buffer = Signal(6)


        m.d.comb += [
            self.scheduler.readStatus.eq(switch_buffer),
            Cat(led[0], led[1], led[2], led[3], led[4], led[5], led[6], led[7]).eq(led_buffer)
        ]

        m.d.sync += [
            switch_buffer.eq(Cat(switch, switch_buffer[1:4])), # just shift an address in
            led_buffer.eq(Cat(self.scheduler.outStatus, self.scheduler.outCptr))
        ]

        return m


if __name__ == "__main__":
    platform = DE10NanoPlatform()
    platform.build(Pipeline(Impl(), Arch()))
