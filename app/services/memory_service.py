class MemoryService:
    def __init__(self):
        self.store = {}

    def get_history(self, sender: str):
        return self.store.get(sender, [])

    def append(self, sender: str, role: str, content: str):
        if sender not in self.store:
            self.store[sender] = []
        self.store[sender].append({"role": role, "content": content})

memory_service = MemoryService()