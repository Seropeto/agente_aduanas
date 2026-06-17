# Diagrama de procesos — AgentIA Aduanas

Recorrido completo de una consulta, desde que el usuario la escribe hasta la respuesta
fundamentada.

```
   Usuario escribe la pregunta
            │
            ▼
   [Portero] verifica el acceso (login JWT)
            │
            ▼
   [SmartRouter] entiende la intención  ─────► elige modelo y estrategia
            │                                    (SIMPLE → económico /
            │                                     COMPLEX → mayor capacidad + PostgreSQL)
            ▼
   [OpenAI] traduce la pregunta a "significado" (embedding)
            │
            ▼
   ¿Está en caché semántica?  ──Sí──►  responde al instante
            │ No
            ▼
   [pgvector] busca la normativa más parecida (por significado)
            │
            ▼
   ¿Encontró ley aplicable?
       │                        │
      Sí (Capa 1)              No
       │                   ┌────┴────────────────┐
       │              ¿pregunta general?   ¿operación específica
       │                   │                 sin datos cargados?
       │              Capa 2 (orientativo)   Capa 3 (protocolo de cortesía)
       │                   │
       └─────────┬─────────┘
                 ▼
   [Cálculo determinista] impuestos con calculadora (si hay montos)
                 │
                 ▼
   [Claude] redacta la respuesta con fundamento + fuentes
                 │
                 ▼
   Respuesta al usuario  ──►  exportable a PDF + guardada en memoria/caché
```

## Leyenda de componentes

| Componente | Responsabilidad |
|---|---|
| **Portero (JWT)** | Autenticación y control de acceso |
| **SmartRouter** | Clasificación de intención y despacho de modelo |
| **OpenAI Embeddings** | Conversión de texto a vector de significado (1536 dims) |
| **Caché semántica** | Reuso de respuestas a consultas semánticamente equivalentes (pgvector) |
| **pgvector** | Búsqueda vectorial sobre la normativa indexada |
| **Motor 3 capas** | Decide cómo responder: normativa / orientativo / protocolo de cortesía |
| **Cálculo determinista** | Impuestos por fórmula legal exacta (nunca por la IA) |
| **Claude** | Redacción final con fundamento y citas |

> Este diagrama es la versión textual (ASCII). Una versión renderizada como imagen
> puede colocarse en `../assets/diagrama-procesos.png`.
