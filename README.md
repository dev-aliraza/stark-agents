# Stark Agents
An SDK to build AI agents

## Installation

```sh
# install from PyPI
pip install stark-agents
```

## Usage

```python
from stark.agent import Agent

input = {"role": "user", "content": "Hi, how are you?"}

agent = Agent(
    ...
)

result = Runner(agent).run(input=input)

print(result)
```