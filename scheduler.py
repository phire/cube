from nmigen import *
from nmigen.cli import main
from multiMem import MultiMem
from renamer import Renamer

class Scheduler(Elaboratable):
    # Takes the output from renamer, stores it in a queue until all dependencies are met
    # and then issues it to the execution units

    # TODO: The current implementation uses the renamingID diredtly as the queueID
    #       This is simple, but wastes a bunch of space in scheduler/rob structures for the renaming
    #       registers backing arch registers of completed instructions

    def __init__(self, Impl, Arch, renamer: Renamer):
        self.width = width = Impl.numRenamingRegisters.bit_length()
        self.NumIssues = Impl.NumIssues

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

        # First, we need to find conflicts between writes to MT

        def accumulateConflcits(Result: Signal, ThisId: Signal, chain: Value, start: int, name):
            # Look at every arg after this one in the wave and check if any depend on the same ID
            final = chain
            for j in range(start + 1, self.NumIssues):
                conflictA = Signal(name=f"{name}_conflicted_by_{j}A")
                conflictB = Signal(name=f"{name}_conflicted_by_{j}B")

                accumulated = Signal()

                m.d.comb += [
                    # check if the IDs are equal
                    conflictA.eq(ThisId == self.renamer.outA[j]),
                    conflictB.eq(ThisId == self.renamer.outB[j]),

                    # and acclumuate the result in a chain
                    accumulated.eq(final | conflictA | conflictB)
                ]

                final = accumulated
            m.d.comb += Result.eq(~final) # invert

        def sumPrecedingConflicts(Result: Signal, ThisId: Signal, end: int, name):
            # look at all args before this one and count how many conflcit
            # Clamp at two

            # I'm hoping this can be packed into one or two LUTs
            finalSum = Const(0)
            for j in range(0, end):
                conflictA = Signal(name=f"{name}_conflicted_by_{j}A")
                conflictB = Signal(name=f"{name}_conflicted_by_{j}B")
                conflict = Signal(name=f"{name}_conflicted_by_{j}")

                m.d.comb += [
                    conflictA.eq(ThisId == self.renamer.outA[j]),
                    conflictB.eq(ThisId == self.renamer.outB[j]),
                    conflict.eq(conflictA | conflictB)
                ]

                sum = Signal(4) # just assume NumIssues will never exceed 15
                m.d.comb += sum.eq(finalSum + conflict)
                finalSum = sum

            with m.If(finalSum > Const(2)):
                m.d.comb += Result.eq(Const(2))
            with m.Else():
                m.d.comb += Result.eq(finalSum)

        # These signals allow the status of a new MT entry to be set to 0 on create.
        # If another uop in this same wave depends on it, the status will need to be set to something else
        AllowStatusCreate = [Signal(name = f"allow_status_create_{i}") for i in range(self.NumIssues)]
        for i in range(self.NumIssues):
            accumulateConflcits(AllowStatusCreate[i], self.renamer.outOut[i], Const(0), i, f"uop{i}_create")

        # For each arg, track if this is the first, second or Nth use of that dependenciy this wave
        # Later uses might need to skip straght to another stage of depending
        DependConflictOffset = [Signal(2, name = f"deopend_conflict_offset_{i}", reset=1) for i in range(self.NumIssues * 2)]

        # And track if this is the last update
        AllowStatusUpdate = [Signal(name = f"allow_status_update_{i}") for i in range(self.NumIssues * 2)]

        for i in range(self.NumIssues):
            accumulateConflcits(AllowStatusUpdate[i*2    ], self.renamer.outA[i], Const(0), i, f"uop{i}_argA_update")
            accumulateConflcits(AllowStatusUpdate[i*2 + 1], self.renamer.outB[i], Const(0), i, f"uop{i}_argB_update")

            sumPrecedingConflicts(DependConflictOffset[i*2    ], self.renamer.outA[i], i, f"uop{i}_argA_update")
            sumPrecedingConflicts(DependConflictOffset[i*2 + 1], self.renamer.outB[i], i, f"uop{i}_argB_update")

        wPORT = 0
        c_wPORT = 0
        rPORT = 0

        # for each uop, create a entry in MT
        for i in range(self.NumIssues):
            m.d.comb += [
                self.MappingTableStatus.write_addr[wPORT].eq(self.renamer.outOut[i]),
                self.MappingTableStatus.write_data[wPORT].eq(Const(0)), # clear to no dependices

                 # but only if there isn't a conflict (another uop is reading this result this cycle)
                self.MappingTableStatus.write_enable[wPORT].eq(AllowStatusCreate[i])
            ]

            # Update used memory ports
            wPORT += 1

            ArgsNotEqual = Signal(name=f"uop{i}_ArgsNotEqual")

            # if the args both read the same source, we only need to update it once
            m.d.comb += ArgsNotEqual.eq(self.renamer.outA[i] != self.renamer.outB[i])

            # for each arg
            for j, arg in enumerate([self.renamer.outA[i], self.renamer.outB[i]]):
                nnn = "AB"[j]
                PrevStatus = Signal(3, name=f"uop{i}_{nnn}_status")

                WriteCptr = Signal(name=f"uop{i}_{nnn}_write_cptr")
                WriteMptr = Signal(name=f"uop{i}_{nnn}_write_mptr")
                WriteStatus = Signal(name=f"uop{i}_{nnn}_write_status")

                # Suppress all writes if this is the first arg and both args are equal
                if j == 0:
                    m.d.comb += [
                        WriteCptr.eq((PrevStatus == Const(0)) & ArgsNotEqual),
                        WriteMptr.eq((PrevStatus == Const(1)) & ArgsNotEqual),
                        WriteStatus.eq(AllowStatusUpdate[i*2] & ArgsNotEqual)
                    ]
                else:
                    m.d.comb += [
                        WriteCptr.eq(PrevStatus == Const(0)),
                        WriteMptr.eq(PrevStatus == Const(1)),
                        WriteStatus.eq(AllowStatusUpdate[i*2 + 1])
                    ]

                m.d.comb += [
                    # Read the pervious status
                    self.MappingTableStatus.read_addr[rPORT].eq(arg),
                    PrevStatus.eq(self.MappingTableStatus.read_data[rPORT] + DependConflictOffset[i*2+j]),

                    # If it has no dependencies, write our C-Pointer
                    self.MappingTableCptr.write_enable[c_wPORT].eq(WriteCptr),
                    self.MappingTableCptr.write_addr[c_wPORT].eq(arg),
                    self.MappingTableCptr.write_data[c_wPORT].eq(self.renamer.outOut[i]),

                    # TODO: If it has one dependencies, write a new M-Pointer

                    # Update the status
                    self.MappingTableStatus.write_enable[wPORT].eq(WriteStatus),
                    self.MappingTableStatus.write_addr[wPORT].eq(arg),
                    # 0 -> 1, 1 -> 2, 2 -> 2, 3 -> 2, 4 -> 2
                    self.MappingTableStatus.write_data[wPORT].eq(Const(0b1010101001).word_select(PrevStatus, 2))
                ]

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



