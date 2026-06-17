# AgentIA Aduanas — Vitrina Técnica

> Asistente experto de comercio exterior y compliance aduanero para Chile, construido
> sobre una arquitectura **RAG Híbrida de 3 capas** con enrutamiento adaptativo,
> fallback cognitivo y cálculo tributario determinista.
>
> **Este repositorio es documentación arquitectónica.** El código fuente del motor
> transaccional se mantiene en repositorios privados (ver disclaimer al pie).

---

## 1. El problema y el modelo de negocio

La clasificación arancelaria y el cálculo de tributos de importación en Chile son
tareas de alto riesgo: un error de clasificación o un impuesto mal calculado tienen
consecuencias legales y económicas reales. Hoy dependen de la consulta manual de
normativa dispersa (Arancel Aduanero, DL 825, tratados de libre comercio, resoluciones
del SNA) y del criterio de profesionales escasos.

**AgentIA Aduanas** entrega consultas fundamentadas en la normativa real —citando la
fuente— y separa con transparencia lo que es *dato legal verificado* de lo que es
*conocimiento general orientativo*. El cálculo de impuestos nunca se delega a la IA:
se computa de forma determinista con la fórmula legal exacta.

El modelo de negocio es **SaaS B2B** orientado a agencias de aduana, importadores y
asesores de comercio exterior, con planes por volumen de consultas y carga de
documentación propia (carpetas de despacho) para auditoría asistida.

---

## 2. Arquitectura de alto nivel

```
   Usuario (chat web autenticado)
            │
            ▼
   FastAPI (backend async)  ──  Autenticación JWT
            │
            ▼
   SmartRouter  ──►  clasifica intención (SIMPLE / COMPLEX) y despacha modelo
            │
            ▼
   Embeddings (OpenAI)  ──►  caché semántica (pgvector)
            │
            ▼
   Búsqueda vectorial (PostgreSQL + pgvector)
            │
            ▼
   Motor RAG Híbrido de 3 capas
            │
            ▼
   Redacción (Anthropic Claude)  +  Cálculo tributario determinista
            │
            ▼
   Respuesta fundamentada  ──►  exportable a PDF
```

---

## 3. El Motor RAG Híbrido de 3 capas

El corazón del sistema. Decide **cómo** responder según lo que encuentra en la base
de conocimiento:

| Capa | Cuándo actúa | Qué hace | Garantía |
|---|---|---|---|
| **Capa 1 — Normativa** | La consulta coincide con la ley indexada | Responde citando la norma exacta | Máxima confiabilidad, con fuente |
| **Capa 2 — Conocimiento experto** | No hay coincidencia, pero la pregunta es general/normativa | Usa el conocimiento del modelo, **etiquetado como orientativo** | Transparencia: avisa que requiere verificación |
| **Capa 3 — Protocolo de cortesía** | Se pregunta por una operación específica sin datos cargados | Devuelve un mensaje determinista pidiendo la documentación de respaldo | No inventa datos sobre operaciones inexistentes |

**Cálculo tributario determinista (ortogonal a las 3 capas):** siempre que la consulta
involucra montos, un módulo de código —no la IA— calcula los impuestos con la fórmula
legal exacta:

```
Derecho Ad Valorem = 6% × valor CIF        (parametrizable según TLC aplicable)
Base del IVA       = CIF + Derecho Ad Valorem
IVA                = 19% × (CIF + Derecho)  (DL 825, Art. 16 letra a)
```

Esto garantiza que **los números nunca los "inventa" el modelo de lenguaje**.

---

## 4. Enrutamiento adaptativo (SmartRouter)

Cada consulta se clasifica antes de procesarse, para usar el recurso óptimo:

- **SIMPLE** (definiciones, conceptos, clasificación genérica) → modelo rápido y
  económico.
- **COMPLEX** (auditoría de documentos del usuario, cruces relacionales, rangos de
  fechas/folios) → modelo de mayor capacidad + consulta a PostgreSQL filtrada por
  usuario.

El despacho lo decide un **pipeline determinista en código**, no un agente autónomo:
una decisión de diseño deliberada para preservar **auditabilidad** en un dominio
tributario/legal.

---

## 5. Stack tecnológico

| Capa | Tecnología |
|---|---|
| Interfaz | HTML + CSS + JavaScript (chat SPA) |
| Backend | Python + FastAPI (async-native) |
| Base de conocimiento | PostgreSQL 16 + **pgvector** (búsqueda semántica, 1536 dims) |
| Embeddings | OpenAI `text-embedding-3-small` |
| Generación / razonamiento | Anthropic Claude (despacho multi-modelo) |
| Memoria conversacional | SQLite (historial por usuario con resumen automático) |
| Autenticación | JWT |
| Infraestructura | Docker + orquestador de despliegue continuo |
| Ingesta de normativa | Scrapers + tareas programadas (APScheduler) |

---

## 6. Superficie de API

El backend expone una API REST documentada con **OpenAPI / Swagger UI** en `/docs`.
Endpoints principales (demostrativos):

| Método | Ruta | Descripción |
|---|---|---|
| `GET`  | `/health` | Healthcheck del servicio |
| `POST` | `/api/auth/login` | Autenticación y emisión de JWT |
| `POST` | `/api/chat/stream` | Consulta al motor RAG (respuesta en streaming) |
| `POST` | `/api/documents/upload` | Carga de documentación propia (carpetas de despacho) |
| `GET`  | `/docs` | Documentación interactiva Swagger UI |

> 📸 _Capturas de Swagger UI demostrando los endpoints: ver carpeta_ `assets/`.

---

## 7. Demostración de IaC

El archivo [`docker-compose.demo.yml`](docker-compose.demo.yml) ilustra el dominio de
Infraestructura como Código del proyecto: orquestación de servicios, healthchecks,
gestión de dependencias entre contenedores e inyección de secretos por variables de
entorno.

```bash
docker compose -f docker-compose.demo.yml up -d
```

> ⚠️ Es una versión **sanitizada y de demostración**. La topología de red, los alias
> internos y la configuración de producción se mantienen privados.

---

## 8. Garantías de diseño

- Respuestas **fundamentadas en normativa cargada**, con fuentes citadas.
- **Cálculos de impuestos exactos**, hechos por código y no por la IA.
- Marca explícita de **cuándo** una respuesta es orientativa vs. dato verificado.
- **No reemplaza** el juicio de un agente de aduanas o asesor legal: toda respuesta es
  de carácter referencial.

---

> **Aviso de Propiedad Intelectual:** El código fuente del motor transaccional
> (motor RAG híbrido, capa de cálculo determinista, lógica de inyección y la
> infraestructura de producción) se mantiene en repositorios privados por políticas de
> protección de Propiedad Intelectual. Este repositorio sirve exclusivamente como
> **documentación arquitectónica** para evaluación técnica y comercial.

---

*AgentIA Aduanas — Toxiro Digital.*
