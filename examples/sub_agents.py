from stark import Agent, Runner

def main():

    agent_3 = Agent(
        name="Delivery-Agent",
        description="This agent will be used for delivering pizza", # Sub agent must have a description.
        instructions="Say `pizza has been delivered` in your response and add pizza type and temperature if available",
        model="gemini-3-pro-preview"
    )

    
    agent_2 = Agent(
        name="Pizza-Agent",
        description="This agent will be used for baking pizza", # Sub agent must have a description.
        instructions="Say 'pizza has been baked at 60 degree' in your response, add pizza type in the response if available and call Delivery Agent",
        model="gemini-3-pro-preview"
    )

    agent_1 = Agent(
        name="Master-Agent",
        instructions="You have sub_agent tool. Call tools based on user query",
        model="gemini-3-pro-preview",
        sub_agents=[agent_2, agent_3]
    )

    result = Runner(agent_1).run(input=[{ "role": "user", "content": "I need to order peperoni pizza" }])

    print(result)
    print("")
    print(result.sub_agents_response.get("Pizza-Agent"))
    print("")
    print(result.sub_agents_response.get("Delivery-Agent"))

main()