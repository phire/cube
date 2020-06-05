from nmigen import *
from nmigen.cli import main

class MultiMem(Elaboratable):
    def __init__(self, width, depth, readPorts, writePorts):
        self.mem = [ [ Memory(width=width, depth=depth, name=f"mem_{i}_{j}") for i in range(writePorts)] for j in range(readPorts)]

        # read ports
        self.read_addr = [ Signal(depth.bit_length(), name="read_addr" + str(i)) for i in range(readPorts)]
        self.read_data = [ Signal(width.bit_length(), name="read_data" + str(i)) for i in range(readPorts)]

        # write ports
        self.write_addr = [ Signal(depth.bit_length(), name="write_addr" + str(i)) for i in range(writePorts)]
        self.write_enable = [ Signal(name="write_en" + str(i)) for i in range(writePorts)]
        self.write_data = [ Signal(width.bit_length(), name="write_data" + str(i)) for i in range(writePorts)]

        # we need a small bit of true multiport ram for the live value table
        self.lvt = Memory(width=writePorts.bit_length(), depth=depth, name="lvt")

        self.readPorts = readPorts
        self.writePorts = writePorts
        self.width = width

    def elaborate(self, platform):
        m = Module()

        for j in range(self.writePorts):
            m.submodules["mem_" + chr(ord('a') + j) + "_lvt_write"] = lvt_wport = self.lvt.write_port()

            m.d.comb += [
                lvt_wport.en.eq(self.write_enable[j]),
                lvt_wport.addr.eq(self.write_addr[j]),
                lvt_wport.data.eq(Const(j))
            ]


        for i in range(self.readPorts):
            # Hold the output of the read mems until we select the right one.
            read_data_buffer = Array(Signal(self.width.bit_length(), name=f"read_temp_{i}_{j}") for j in range(self.writePorts))

            for j in range(self.writePorts):
                name = "mem_" + str(i) + chr(ord('a') + j)

                m.submodules[name + "_read"] = read_port = self.mem[i][j].read_port()
                m.submodules[name + "_write"] = write_port = self.mem[i][j].write_port()

                m.d.comb += [
                    # Set write ports
                    write_port.addr.eq(self.write_addr[j]),
                    write_port.data.eq(self.write_data[j]),
                    write_port.en.eq(self.write_enable[j]),

                    # Set read port address
                    read_port.addr.eq(self.read_data[i]),

                    # move read result into holding array
                    read_data_buffer[Const(j)].eq(read_port.data),
                ]

            m.submodules[f"mem_{i}_lvt"] = lvt_rport = self.lvt.read_port()
            m.d.comb += [
                # Query Live Value Table for which memory has the correct result
                lvt_rport.addr.eq(self.read_addr[i]),

                # Grab correct result out of holding array based on lvt
                self.read_data[i].eq(read_data_buffer[lvt_rport.data])
            ]

        return m


if __name__ == "__main__":
    mm = MulitMem(32, 128, 3, 3)
    ports = []
    for i in range(3):
        ports += [mm.read_addr[i], mm.read_data[i]]

    for i in range(3):
        ports += [mm.write_addr[i], mm.write_enable[i], mm.write_data[i]]

    main(mm, ports = ports)





