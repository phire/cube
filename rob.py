from nmigen import *
import MultiMem

# This paper has some ideas for "optimizing" this design for FPGAs by not having true multiport ram
# https://www.researchgate.net/publication/241628101_An_out-of-order_superscalar_processor_on_FPGA_The_ReOrder_Buffer_design

class ReorderBuffer(Elaboratable):
    def __init__(self, numDecodes, numIssues, numFinalizes, numExecutions, numEntries):

        executeDataWidth = 5 + 16 # operand + 16bit immediate
        # One read port per numIssues, one write port per numDecodes
        self.executedata = MultiMem(width=executeDataWidth, depth=numEntries, numIssues, numDecodes)

    def elaborate(self, platform):
        m = Module()

        m.submodules.executedata = self.executedata

        # 1. Every cycle, take output from renaming and write it to the various buffers



        # 2. Every cycle, work out which uops will can be issued next cycle
        #    We need some kind of priority system



        # 3. Every cycle, issue the ready uops



        # 4. If there is an Exception or missprediction, somehow evict invalided nodes
        #    (I suspect this should be as simple as moving circular buffer pointers)

        return m


if __name__ == "__main__":
    rob = ReorderBuffer(32, 128, 3, 3)

    main(rob, ports = [])




