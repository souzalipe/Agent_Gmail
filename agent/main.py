from agno.agent import Agent
from agno.models.groq import Groq
from dotenv import load_dotenv

# Carrega variáveis do .env
load_dotenv()

# Criando agente
agent = Agent(
    model=Groq(id="llama-3.3-70b-versatile"),
    markdown=True,
    description="Você é um assistente especialista em tecnologia."
)

# Executa pergunta
agent.print_response(
    "Explique o que é inteligência artificial"
)