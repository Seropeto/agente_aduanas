# 🤖 Agente Aduanas — IA especializada en normativa aduanera chilena

> Consulta normativa oficial + documentos internos de tu empresa con IA especializada en comercio exterior.

🔗 **Demo en vivo:** [aduanas.toxirodigital.cloud](https://aduanas.toxirodigital.cloud)

---

## ¿Qué hace?

**Agente Aduanas** es un agente de inteligencia artificial basado en arquitectura RAG 
(Retrieval-Augmented Generation) que combina dos fuentes de conocimiento en una sola consulta:

1. **Normativa oficial chilena** — Aduana Chile, SII, BCN y Diario Oficial, siempre actualizada
2. **Documentos internos de la empresa** — procedimientos, contratos y manuales subidos por el usuario

A diferencia de un chatbot genérico, el agente responde con contexto real de la operación 
del cliente, no con respuestas genéricas.

---

## Problema que resuelve

Los equipos de comercio exterior pierden horas buscando manualmente en portales de gobierno, 
mientras sus procedimientos internos permanecen dispersos en PDFs que nadie encuentra. 
Información desactualizada = multas y retrasos.

---

## Stack tecnológico

| Capa | Tecnología |
|------|------------|
| Backend | Python, FastAPI |
| Motor RAG | ChromaDB (base vectorial) + Claude API (Anthropic) |
| OCR | Tesseract |
| Frontend | HTML, CSS, JavaScript |
| Infraestructura | Docker, Docker Compose, VPS Linux |
| Fuentes normativas | Aduana Chile, SII, BCN, Diario Oficial |

---

## Arquitectura# agente_aduanas
