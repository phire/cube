from nmigen import *
from nmigen.cli import main
from multiMem import MultiMem
from decoder import Decoder

class Arch:
    NumGPR = 32

class Impl:
    NumDecodes = 4
    numIssues = 4
    numFinalizes = 4
    numExecutions = 4
    numEntries = 128
    numRenamingRegisters = 150

class Renamer(Elaboratable):
    # Tracks which renaming register contains each architectural register in the RAT
    # converts arch register IDs to renaming register IDs

    def __init__(self, Impl, Arch):
        self.width = width = Impl.numRenamingRegisters.bit_length()

        self.decoders = [ Decoder() for _ in range(Impl.NumDecodes)]
        self.gprRAT = MultiMem(
            width=width,
            depth=Arch.NumGPR,
            readPorts=Impl.NumDecodes*2, # Every decode might output 2 reads
            writePorts=Impl.NumDecodes)  # Every decode might output 1 writes

        self.allocated = [Signal(width, name=f"allocated_{i}") for i in range(Impl.NumDecodes)]
        self.isAllocated = [Signal(name=f"isAllocated_{i}") for i in range(Impl.NumDecodes)]
        self.updateEnabled = [Signal(name=f"updateEnabled_{i}") for i in range(Impl.NumDecodes)]

        self.nextFreeRegister = Signal(width, reset=0)

        # outputs
        self.outA = [Signal(width, name=f"outA_{i}") for i in range(Impl.NumDecodes)]
        self.outB = [Signal(width, name=f"outB_{i}") for i in range(Impl.NumDecodes)]
        self.outOut = [Signal(width, name=f"outOut_{i}") for i in range(Impl.NumDecodes)]


    def elaborate(self, platform):
        m = Module()

        # Register submodules
        m.submodules.gprRAT = self.gprRAT
        for i, decoder in enumerate(self.decoders):
            m.submodules[f"decoder{i}"] = decoder

        allocatedCount = Const(0)
        # Allocate a renaming register for each uop which needs it
        for i, decoder in enumerate(self.decoders):
            oldAllocatedCount = allocatedCount
            allocatedCount = Signal(self.width, name=f"decoder_{i}_allocate_counter")
            m.d.comb += [
                # if the uop writes to a register, then we need to allocate
                self.isAllocated[i].eq(decoder.valid[2]),

                # Allocate the next register
                # Will contain junk when this uop doesn't allocate
                self.allocated[i].eq(self.nextFreeRegister + oldAllocatedCount),

                # Keep track of how many we have allocated this cycle
                allocatedCount.eq(oldAllocatedCount + self.isAllocated[i])
            ]

         # Increment the free register pointer
        m.d.sync += self.nextFreeRegister.eq(self.nextFreeRegister + allocatedCount)


        conflictables = []

        # Every cycle, pull all read arch registers from the decoders and look them up in RAT
        for i, decoder in enumerate(self.decoders):
            regA_rat = Signal(self.width, name=f"decoder{i}_regA_RAT")
            regB_rat = Signal(self.width, name=f"decoder{i}_regB_RAT")

            # Read RAT entries for each input register
            m.d.comb += [
                self.gprRAT.read_addr[i * 2    ].eq(decoder.regA),
                self.gprRAT.read_addr[i * 2 + 1].eq(decoder.regB),
                regA_rat.eq(self.gprRAT.read_data[i * 2    ]),
                regB_rat.eq(self.gprRAT.read_data[i * 2 + 1])
            ]

            regA_final = regA_rat
            regB_final = regB_rat

            # check for conflicts
            for archRegId, decoderId in conflictables:
                dependsA = Signal(name=f"decoder{i}A_depends_on_{decoderId}out")
                dependsB = Signal(name=f"decoder{i}B_depends_on_{decoderId}out")

                outA = Signal(self.width, name=f"decoder{i}A_resloved{decoderId}")
                outB = Signal(self.width, name=f"decoder{i}B_resloved{decoderId}")
                m.d.comb += [
                    # Check if input registers match the output of a previous uop this cycle
                    dependsA.eq((decoder.regA == archRegId) & self.isAllocated[decoderId]),
                    dependsB.eq((decoder.regB == archRegId) & self.isAllocated[decoderId]),

                    # select correct renaming id
                    outA.eq(Mux(dependsA, self.allocated[decoderId], regA_final)),
                    outB.eq(Mux(dependsB, self.allocated[decoderId], regB_final))
                ]

                # Accumulate mux chains
                regA_final = outA
                regB_final = outB

            m.d.sync += [
                self.outA[i].eq(regA_final),
                self.outB[i].eq(regB_final),
                self.outOut[i].eq(self.allocated[i])
            ]

            # Each decoder down the chain needs to check for more and more conflicts
            conflictables += [(decoder.regOut, i)]

        # Find which outputs are written to more than once in a single cycle
        # We need to update the RAT only once for each ArchReg
        suppressables = []
        for i, decoder in reversed(list(enumerate(self.decoders))):

            enableChain = self.isAllocated[i]

            # This works in reverse to conflicts. the last uop can't be suppressed by anything
            for archRegId, decoderId in suppressables:
                enabled = Signal(name=f"is_{i}_enabled{decoderId}")
                m.d.comb += enabled.eq((~self.isAllocated[decoderId] | decoder.regOut != archRegId) & enableChain)
                enableChain = enabled

            m.d.comb += self.updateEnabled[i].eq(enableChain)
            suppressables += [(decoder.regOut, i)]

        # Update RAT with all write arch registers
        for i, decoder in enumerate(self.decoders):
            m.d.comb += [
                self.gprRAT.write_addr[i].eq(decoder.regOut),
                self.gprRAT.write_data[i].eq(self.allocated[i]),
                self.gprRAT.write_enable[i].eq(self.updateEnabled[i])
            ]

        # TODO: Write some kind of data structure to allow rewinding

        return m

from nmigen.back.pysim import *

def printState(renamer):
    print("-- Cycle --")
    for i in range(4):
        outA = (yield renamer.outA[i])
        outB = (yield renamer.outB[i])
        outOut = (yield renamer.outOut[i])
        update = yield renamer.updateEnabled[i]
        print(f"\tadd {outOut}, {outA}, {outB} -- {update}")

if __name__ == "__main__":
    renamer = Renamer(Impl(), Arch())

    with Simulator(renamer) as sim:
        def process():
            for _ in range(10):
                yield Tick()
                yield from printState(renamer)
        sim.add_clock(0.0001)
        sim.add_process(process)
        sim.run()

    ports = []

    for i in range(Impl().NumDecodes):
        ports += [renamer.outA[i], renamer.outB[i], renamer.outOut[i]]

    main(renamer, ports)





