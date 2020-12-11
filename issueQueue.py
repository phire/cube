from nmigen import *
from nmigen.cli import main
from multiMem import MultiMem
from renamer import Renamer

class IssueQueue(Elaboratable):
    def __init__(self, Impl, Arch):
