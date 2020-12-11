from nmigen import *
from nmigen.cli import main
from multiMem import MultiMem
from renamer import Renamer

class Scheduler(Elaboratable):
    # Takes the output from renamer, stores it in a queue until all dependencies are met
    # and pushs them out to a ready queue

    # TODO: The current implementation uses the renamingID diredtly as the queueID
    #       This is simple, but wastes a bunch of space in scheduler/rob structures for the renaming
    #       registers backing arch registers of completed instructions

    def __init__(self, Impl, Arch):
        self.width = width = Impl.numRenamingRegisters.bit_length()
        self.NumIssues = Impl.NumIssues
        self.NumDecodes = Impl.NumDecodes
        self.NumWakeupChecks = Impl.NumDecodes

        # Inputs from renamer

        self.inA = [Signal(width, name=f"inA_{i}") for i in range(Impl.NumDecodes)]
        self.inB = [Signal(width, name=f"inB_{i}") for i in range(Impl.NumDecodes)]
        self.inOut = [Signal(width, name=f"inOut_{i}") for i in range(Impl.NumDecodes)]
        self.inValid = [Signal(name=f"inValid_{i}") for i in range(Impl.NumDecodes)]

        # Mapping table as described in "Direct Instruction Wakeup for Out-of-Order Processors" (iwia04.pdf)

        # Status, tracks how many depentants each uop has
        # This needs way more reads/writes than other parts of the table
        self.MappingTableStatus = MultiMem(
            width=2, # 0 = No dependents, 1 = One dependent, 2 = Multiple dependents, 3 = completed
            depth=Impl.numRenamingRegisters,
            readPorts=Impl.NumDecodes * 2 + self.NumWakeupChecks, # Each new uop needs to update the status of both it's arguments.
                                                            # Each wakeup check needs to check the status it's other arg
            writePorts=Impl.NumDecodes * 3 + Impl.NumIssues,  # Each new uop needs to set the new status of itself and both it's arguments
                                                              # Each issue needs to update the status to completed
            init=[3] * Impl.numRenamingRegisters)

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

        # uop args
        self.UopArgs = MultiMem(
            width=width * 2,
            depth=Impl.numRenamingRegisters,
            readPorts=self.NumWakeupChecks, # each wakeup check requires one read
            writePorts=Impl.NumDecodes)

        # # Multiple Wake-up Table
        # # TODO: Find optimal size based on target code
        # # Currently we have one write port
        # # TODO: Stall when MWT is full
        # self.MulipleWakeupTable = MultiMem(
        #     width = Impl.numRenamingRegisters,
        #     depth = Impl.MWTSize,
        #     readPorts = 2, # One for updating, one for issue
        #     writePorts = 2)


        self.wakeUpNext = [Signal(width, name=f"ready{i}") for i in range(self.NumWakeupChecks)]
        self.wakeUpNextSrc = [Signal(width, name=f"ready{i}") for i in range(self.NumWakeupChecks)]

        #
        self.readStatus = Signal(Impl.numRenamingRegisters.bit_length())

        # Outputs

        # TODO: Should the number of readyies be equal to decode width
        self.ready = [Signal(width, name=f"ready{i}") for i in range(self.NumWakeupChecks)]
        self.readyValid = [Signal(name=f"ready{i}_valid") for i in range(self.NumWakeupChecks)]

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
                    conflictA.eq(ThisId == self.inA[j]),
                    conflictB.eq(ThisId == self.inB[j]),

                    # and acclumuate the result in a chain
                    accumulated.eq(final | conflictA | conflictB)
                ]

                final = accumulated
            m.d.comb += Result.eq(~final) # invert

        def accumulateConflcitsReverse(Result: Signal, ThisId: Signal, chain: Value, end: int, name):
            # Look at every uop before this one in the wave and check if this arg conflicts with it
            final = chain
            for j in range(0, end):
                conflictA = Signal(name=f"{name}_conflicts_{j}")

                accumulated = Signal()

                m.d.comb += [
                    # check if the IDs are equal
                    conflictA.eq(ThisId == self.inOut[j]),

                    # and acclumuate the result in a chain
                    accumulated.eq(final | conflictA)
                ]

                final = accumulated
            m.d.comb += Result.eq(final)



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
                    conflictA.eq(ThisId == self.inA[j]),
                    conflictB.eq(ThisId == self.inB[j]),
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
            accumulateConflcits(AllowStatusCreate[i], self.inOut[i], Const(0), i, f"uop{i}_create")

        # For each arg, track if this is the first, second or Nth use of that dependenciy this wave
        # Later uses might need to skip straght to another stage of depending
        DependConflictOffset = [Signal(2, name = f"depend_conflict_offset_{i}") for i in range(self.NumIssues * 2)]

        # And track if this is the last update
        AllowStatusUpdate = [Signal(name = f"allow_status_update_{i}") for i in range(self.NumIssues * 2)]

        # Ignore old status if we are creating in the same cycle
        IgnoreStatus = [Signal(name = f"ignore_old_status_{i}") for i in range(self.NumIssues * 2)]

        for i in range(self.NumIssues):
            accumulateConflcits(AllowStatusUpdate[i*2    ], self.inA[i], Const(0), i, f"uop{i}_argA_update")
            accumulateConflcits(AllowStatusUpdate[i*2 + 1], self.inB[i], Const(0), i, f"uop{i}_argB_update")

            sumPrecedingConflicts(DependConflictOffset[i*2    ], self.inA[i], i, f"uop{i}_argA_update")
            sumPrecedingConflicts(DependConflictOffset[i*2 + 1], self.inB[i], i, f"uop{i}_argB_update")

            accumulateConflcitsReverse(IgnoreStatus[i*2], self.inA[i], Const(0), i, f"uop{i}_argA")
            accumulateConflcitsReverse(IgnoreStatus[i*2 + 1], self.inB[i], Const(0), i, f"uop{i}_argB")

        wPORT = 0
        c_wPORT = 0
        rPORT = 0

        # for each uop, create a entry in MT
        for i in range(self.NumDecodes):
            m.d.comb += [
                self.MappingTableStatus.write_addr[wPORT].eq(self.inOut[i]),
                self.MappingTableStatus.write_data[wPORT].eq(Const(0)), # clear to no dependices

                 # but only if there isn't a conflict (another uop is reading this result this cycle)
                self.MappingTableStatus.write_enable[wPORT].eq(AllowStatusCreate[i] & self.inValid[i]),

                # Also store the arguments of this uop
                self.UopArgs.write_addr[i].eq(self.inOut[i]),
                self.UopArgs.write_data[i].eq(Cat(self.inA[i], self.inB[i])),
                self.UopArgs.write_enable[i].eq(self.inValid[i])
            ]

            # Update used memory ports
            wPORT += 1

            ArgsNotEqual = Signal(name=f"uop{i}_ArgsNotEqual")

            # if the args both read the same source, we only need to update it once
            m.d.comb += ArgsNotEqual.eq(self.inA[i] != self.inB[i])

            # for each arg
            for j, arg in enumerate([self.inA[i], self.inB[i]]):
                nnn = "AB"[j]
                PrevStatus = Signal(2, name=f"uop{i}_{nnn}_status")
                OffsetStatus = Signal(3, name=f"uop{i}_{nnn}_offset_status")

                WriteCptr = Signal(name=f"uop{i}_{nnn}_write_cptr")
                WriteMptr = Signal(name=f"uop{i}_{nnn}_write_mptr")
                WriteStatus = Signal(name=f"uop{i}_{nnn}_write_status")
                AlreadyReady = Signal()

                # Suppress all writes if this is the first arg and both args are equal
                if j == 0:
                    m.d.comb += [
                        WriteCptr.eq(self.inValid[i] & (OffsetStatus == Const(0)) & ArgsNotEqual),
                        WriteMptr.eq(self.inValid[i] & (OffsetStatus == Const(1)) & ArgsNotEqual),
                        WriteStatus.eq(self.inValid[i] & AllowStatusUpdate[i*2] & ArgsNotEqual & ~AlreadyReady)
                    ]
                else:
                    m.d.comb += [
                        WriteCptr.eq(self.inValid[i] & OffsetStatus == Const(0)),
                        WriteMptr.eq(self.inValid[i] & OffsetStatus == Const(1)),
                        WriteStatus.eq(self.inValid[i] & AllowStatusUpdate[i*2 + 1] & ~AlreadyReady)
                    ]

                m.d.comb += [
                    # Read the pervious status
                    self.MappingTableStatus.read_addr[rPORT].eq(arg),
                    # If the arg was created within this same wave, we need to ignore the old stale status
                    PrevStatus.eq(Mux(IgnoreStatus[i*2 + j], Const(0), self.MappingTableStatus.read_data[rPORT])),
                    AlreadyReady.eq(PrevStatus == Const(3)),

                    # Also Take into account prevous conflicting args
                    OffsetStatus.eq(PrevStatus + DependConflictOffset[i*2+j]),

                    # If it has no dependencies, write our C-Pointer
                    self.MappingTableCptr.write_enable[c_wPORT].eq(WriteCptr),
                    self.MappingTableCptr.write_addr[c_wPORT].eq(arg),
                    self.MappingTableCptr.write_data[c_wPORT].eq(self.inOut[i]),

                    # TODO: If it has one dependencies, write a new M-Pointer

                    # Update the status
                    self.MappingTableStatus.write_enable[wPORT].eq(WriteStatus),
                    self.MappingTableStatus.write_addr[wPORT].eq(arg),
                    # 0 -> 1, 1 -> 2, 2 -> 2, 3 -> 2, 4 -> 2
                    self.MappingTableStatus.write_data[wPORT].eq(Const(0b1010101001).word_select(OffsetStatus, 2))
                ]

                # Update used memory ports
                rPORT += 1
                c_wPORT += 1
                wPORT += 1

        # Trigger Wakeups checks for instructions that were issued last cycle
        for i, wakeupId in enumerate(self.wakeUpNext):
            argA = Signal(self.width)
            argB = Signal(self.width)

            # read argument infomation out of memory
            m.d.comb += [
                self.UopArgs.read_addr[i].eq(wakeupId),
                Cat(argA, argB).eq(self.UopArgs.read_data[i])
            ]

            otherArg = Signal(self.width, name=f"wakeup{i}_other_arg")

            # Identify the other argument which wasn't the source of the wakeup
            # Saves us a read port
            with m.If(argA == self.wakeUpNextSrc):
                m.d.comb += otherArg.eq(argB)
            with m.Else():
                m.d.comb += otherArg.eq(argA)

            # check the status of the other argument

            otherArgStatus = Signal(2, name=f"wakeup{i}_other_arg_status")
            otherArgReady = Signal(name=f"wakeup{i}_other_arg_ready")

            nextCptr = Signal(self.width)

            m.d.comb += [
                # Read status
                self.MappingTableStatus.read_addr[rPORT].eq(otherArg),
                otherArgStatus.eq(self.MappingTableStatus.read_data[rPORT]),

                # if status is 3, then it's ready
                otherArgReady.eq(otherArgStatus == Const(3)),

                # check next c-pointer
                self.MappingTableCptr.read_addr[i].eq(wakeupId),
                nextCptr.eq(self.MappingTableCptr.read_data[i])
            ]

            m.d.sync += [
                # if it's ready, then we can queue it
                self.readyValid[i].eq((otherArgReady | otherArg == Const(0)) & wakeupId != Const(0)),
                self.ready[i].eq(wakeupId),

                # Update the mapping table status
                self.MappingTableStatus.write_enable[wPORT].eq(otherArgReady),
                self.MappingTableStatus.write_addr[wPORT].eq(wakeupId),
                self.MappingTableStatus.write_data[wPORT].eq(Const(3)),

                # queue any dependcies for update
                self.wakeUpNext[i].eq(nextCptr),
                self.wakeUpNextSrc[i].eq(wakeupId)
            ]

            rPORT += 1
            wPORT += 1


        # m.d.comb += [
        #     self.MappingTableStatus.read_addr[rPORT].eq(self.readStatus),
        #     self.MappingTableCptr.read_addr[5].eq(self.readStatus),
        #     self.outStatus.eq(self.MappingTableStatus.read_data[rPORT]),
        #     self.outCptr.eq(self.MappingTableCptr.read_data[0])
        # ]

        return m

from nmigen.back.pysim import *



if __name__ == "__main__":
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

    scheduler = Scheduler(Impl(), Arch())

    code = [
        (1, 0, 0),
        (2, 1, 1),
        (3, 2, 2),
        (4, 3, 3),
        (5, 4, 0),
        (6, 5, 0),
        (7, 6, 0),
        (8, 7, 7),
        (9, 8, 8),
        (10, 9, 9),
        (11, 10, 10),
        (12, 11, 11),
        (13, 12, 12),
        (14, 13, 13),
        (15, 14, 14),
        (16, 15, 15),
    ]
    counter = 0

    def printState(scheduler):
        print("-- Cycle --")
        string = "status: "
        for i in range(20):
            s = (yield scheduler.MappingTableStatus.lutMem._array[i])
            string += f"{s} "

        print(string)
        string = "cptr:   "
        for i in range(20):
            s = (yield scheduler.MappingTableCptr.lutMem._array[i])
            string += f"{s} "
        print(string)

        for i in range(len(scheduler.inA)):
            global counter
            if counter < len(code):
                (out, A, B) = code[counter]
                print(f"  scheduling {out}: {A}, {B}")
                yield scheduler.inOut[i].eq(out)
                yield scheduler.inA[i].eq(A)
                yield scheduler.inB[i].eq(B)
                yield scheduler.inValid[i].eq(True)
                counter += 1
            else:
                yield scheduler.inValid[i].eq(False)

        for i in range(len(scheduler.ready)):
            readyValid = (yield scheduler.readyValid[i])
            ready = (yield scheduler.ready[i])
            if readyValid:
                print(f"\t\t{ready} is ready")




    with Simulator(scheduler) as sim:
        def process():

            for i in range(len(scheduler.inValid)):
                yield scheduler.inValid[i].eq(False)

            for _ in range(10):
                yield Tick()
                yield from printState(scheduler)
        sim.add_clock(0.0001)
        sim.add_process(process)
        sim.run()

    ports = [scheduler.stall, scheduler.outStatus, scheduler.outCptr]

    for i in range(Impl().NumDecodes):
        ports += [scheduler.inA[i], scheduler.inB[i], scheduler.inOut[i], scheduler.inValid[i]]

    for i in range(Impl().NumIssues):
        ports += [scheduler.readyValid[i], scheduler.ready[i]]

    main(scheduler, ports)



