"""Orquestación de CommunityLab (LangGraph).

Los módulos se importan de forma explícita (`orchestration.pipeline_runner`,
`orchestration.resultado`, `orchestration.state`, `orchestration.worker`); este
paquete no los importa aquí para que el worker no arrastre LangGraph ni el stack
de IA hasta que realmente los necesite.
"""
