
from abc import ABC, abstractmethod

class Cmd(ABC):
    def __init__(self):
        super().__init__()

    @abstractmethod
    def main(self, argv):
        pass
