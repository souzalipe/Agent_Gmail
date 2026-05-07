from agno.agent import Agent
from agno.models.groq import Groq
from dotenv import load_dotenv

load_dotenv()

agent = Agent(
    model=Groq(
        id="llama-3.3-70b-versatile"
    ),
    markdown=True
)

while True:
    pergunta = input("\nVocê: ")

    if pergunta.lower() == "sair":
        break

    resposta = agent.run(pergunta)

    print("\nAgente:")
    print(resposta.content)