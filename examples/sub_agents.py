from stark import Agent, Runner

def main():

    agent_3 = Agent(
        name="Delivery-Agent",
        description="This agent will be used for delivering pizza", # Sub agent must have a description.
        instructions="Delivery Pizza to customer and return response `Your {{ Pizza Type }} Pizza has been delivered`",
        model="gemini-3-pro-preview"
    )

    
    agent_2 = Agent(
        name="Pizza-Agent",
        description="This agent will be used for baking pizza", # Sub agent must have a description.
        instructions="Bake Pizza at 60 degree and return response `Your {{ Pizza Type }} Pizza is ready`",
        model="gemini-3-pro-preview"
    )

    agent_1 = Agent(
        name="Master-Agent",
        instructions="You're the pizza delivery company agent responsible for baking pizza and deliver them to the customer.",
        model="gemini-3-pro-preview",
        sub_agents=[agent_2, agent_3]
    )

    result = Runner(agent_1).run(input=[{ "role": "user", "content": "I need to order peperoni pizza" }])

    print(result)
    print("")

main()