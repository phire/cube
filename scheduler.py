from nmigen import *
from nmigen.cli import main
from multiMem import MultiMem
from renamer import Renamer

class Scheduler(Elaboratable):
    # Takes the output from renamer, stores it in a queue until all dependencies are met
    # and then issues it to the execution units

    def __init__(self, Impl, Arch, renamer: Renamer):
        self.width = width = Impl.numRenamingRegisters.bit_length()

        # Inputs from renamer

        self.renamer = renamer

        # self.inA = [Signal(width, name=f"inA_{i}") for i in range(Impl.NumDecodes)]
        # self.inB = [Signal(width, name=f"inB_{i}") for i in range(Impl.NumDecodes)]
        # self.inOut = [Signal(width, name=f"inOut_{i}") for i in range(Impl.NumDecodes)]
        # self.inValid = [Signal(name=f"inValid_{i}") for i in range(Impl.NumDecodes)]

        # Mapping table as described in "Direct Instruction Wakeup for Out-of-Order Processors" (iwia04.pdf)

        # Status, tracks how many depentants each uop has
        # This needs way more reads/writes than other parts of the table
        self.MappingTableStatus = MultiMem(
            width=2, # 0 = No dependents, 1 = One dependent, 2 = Multiple dependents, 3 = completed
            depth=Impl.numRenamingRegisters,
            readPorts=Impl.NumDecodes * 2 + Impl.NumIssues, # Each new uop needs to update the status of both it's arguments.
                                                            # Each Issue needs know how many depedents
            writePorts=Impl.NumDecodes * 3 + Impl.NumIssues)  # Each new uop needs to set the new status of itself and both it's arguments
                                                              # Each issue needs to update the status to completed

        # C-Pointer, Tracks the first dependency of each uop
        self.MappingTableCptr = MultiMem(
            width=(Impl.numRenamingRegisters-1).bit_length(),
            depth=Impl.numRenamingRegisters,
            readPorts=Impl.NumIssues, # Only need to read this back at issue
            writePorts=Impl.NumDecodes * 2) # One per dependency

        # # M-Pointer, indirect pointer to the MWT table containing the remaining dependies for this instruction
        # self.MappingTableMptr = MultiMem(
        #     width=(Impl.MWTSize-1).bit_length(),
        #     depth=Impl.numRenamingRegisters,
        #     readPorts=Impl.NumIssues + Impl.NumDecodes * 2, # Need to read at issue AND when appending to MWT
        #     writePorts=Impl.NumDecodes * 2) # One per dependency

        # # Multiple Wake-up Table
        # # TODO: Find optimal size based on target code
        # # Currently we have one write port
        # # TODO: Stall when MWT is full
        # self.MulipleWakeupTable = MultiMem(
        #     width = Impl.numRenamingRegisters,
        #     depth = Impl.MWTSize,
        #     readPorts = 2, # One for updating, one for issue
        #     writePorts = 2)


        #
        self.readStatus = Signal(Impl.numRenamingRegisters.bit_length())

        # Outputs

        self.stall = Signal()
        self.outStatus = Signal(2)
        self.outCptr = Signal(width)


    def elaborate(self, platform):
        m = Module()

        # m.submodules.MulipleWakeupTable = self.MulipleWakeupTable
        m.submodules.MappingTableCptr = self.MappingTableCptr
        # m.submodules.MappingTableMptr = self.MappingTableMptr
        m.submodules.MappingTableStatus = self.MappingTableStatus
        # m.submodules.renamer = self.renamer


        wPORT = 0
        c_wPORT = 0
        rPORT = 0

        # for each uop, create a entry in MT
        for i in range(len(self.renamer.outValid)):
            m.d.comb += [
                self.MappingTableStatus.write_addr[wPORT].eq(self.renamer.outOut[i]),
                self.MappingTableStatus.write_data[wPORT].eq(Const(0)), # clear to no dependices

                 # but only if there isn't a conflict (another uop is reading this result this cycle)
                self.MappingTableStatus.write_enable[wPORT].eq(~self.renamer.outConflict[i])
            ]

            # Update used memory ports
            wPORT += 1

            ArgsNotEqual = Signal(name=f"uop{i}_ArgsNotEqual")

            # if the args both read the same source, we only need to update it once
            m.d.comb += ArgsNotEqual.eq(self.renamer.outA[i] != self.renamer.outB[i])

            # for each arg
            for j, arg in enumerate([self.renamer.outA[i], self.renamer.outB[i]]):
                nnn = "AB"[j]
                PrevStatus = Signal(2, name=f"uop{i}_{nnn}_status")

                m.d.comb += [
                    # Read the pervious status
                    self.MappingTableStatus.read_addr[rPORT].eq(arg),
                    PrevStatus.eq(self.MappingTableStatus.read_data[rPORT]),

                    # If it only no dependencies, write our C-Pointer
                    self.MappingTableCptr.write_enable[c_wPORT].eq(PrevStatus == Const(0)),
                    self.MappingTableCptr.write_addr[c_wPORT].eq(arg),
                    self.MappingTableCptr.write_data[c_wPORT].eq(self.renamer.outOut[i]),

                    # TODO: If it has one dependencie, write a new M-Pointer

                    # Update the status: 0 -> 1, 1 -> 2, 2 -> 2
                    self.MappingTableStatus.write_addr[wPORT].eq(arg),
                    self.MappingTableStatus.write_data[wPORT].eq(Const(0b101001).word_select(PrevStatus, 2))
                ]

                # Suppress updates if this is the first arg and both args are equal
                if j == 0:
                    m.d.comb += self.MappingTableStatus.write_enable[wPORT].eq(ArgsNotEqual)
                else:
                    m.d.comb += self.MappingTableStatus.write_enable[wPORT].eq(Const(1))

                # Update used memory ports
                rPORT += 1
                c_wPORT += 1
                wPORT += 1

        m.d.comb += [
            self.MappingTableStatus.read_addr[rPORT].eq(self.readStatus),
            self.MappingTableCptr.read_addr[0].eq(self.readStatus),
            self.outStatus.eq(self.MappingTableStatus.read_data[rPORT]),
            self.outCptr.eq(self.MappingTableCptr.read_data[0])
        ]

        return m




if __name__ == "__main__":
    class Arch:
        NumGPR = 32

    class Impl:
        NumDecodes = 2
        NumIssues = 2
        numFinalizes = 4
        numExecutions = 4
        numEntries = 128
        numRenamingRegisters = 16
        MWTSize = 16

    renamer = renamer = Renamer(Impl(), Arch())

    scheduler = Scheduler(Impl(), Arch(), renamer)

    ports = [scheduler.stall, scheduler.outStatus, scheduler.outCptr]
    main(scheduler, ports)



