# AgentIA — Especificación de Requerimientos de Software
### Plan de Mejoras — Versión 1.0

| Campo | Detalle |
|---|---|
| **Proyecto** | AgentIA — SaaS Normativo RAG |
| **Empresa** | ToxiroApps / Toxiro |
| **Fecha** | Mayo 2026 |
| **Versión** | 1.0 — Inicial |
| **Clasificación** | Confidencial — Uso Interno |

---

## 1. Introducción

### 1.1 Propósito del Documento

Este documento define los requerimientos funcionales y técnicos para la implementación de seis mejoras estratégicas en la plataforma AgentIA. Constituye la fuente única de verdad para el Equipo de Desarrollo y sirve como referencia para validación, pruebas y entrega de cada módulo.

### 1.2 Contexto del Proyecto

AgentIA es un SaaS de consulta normativa basado en RAG (Retrieval-Augmented Generation), desplegado en VPS Hostinger, orientado a sectores regulados en Chile. El sistema actual utiliza:

- **Base de datos vectorial:** ChromaDB
- **Modelo de lenguaje:** Claude Sonnet via API Anthropic
- **OCR:** Tesseract
- **Frontend:** HTML / CSS / JavaScript vanilla
- **Backend:** Python + Node.js
- **Infraestructura:** VPS Hostinger (único servidor Fase 1)

### 1.3 Alcance

El presente documento cubre las siguientes 6 áreas de mejora, organizadas en 4 fases de implementación:

1. Memoria persistente por usuario
2. Caché semántico
3. Versionado y trazabilidad normativa
4. Salidas múltiples: PDF, Email, WhatsApp
5. Optimización de tiempos de respuesta
6. Ahorro en consumo de tokens

---

## 2. Stack Tecnológico de Referencia

| Componente | Tecnología | Notas |
|---|---|---|
| Servidor | VPS Hostinger | Ubuntu, único nodo Fase 1 |
| BD Vectorial | ChromaDB | Documentos normativos + caché semántico |
| BD Relacional | SQLite | Memoria conversacional + changelog |
| LLM Principal | Claude Sonnet (Anthropic) | Consultas complejas |
| LLM Secundario | Claude Haiku (Anthropic) | Consultas simples / clasificación |
| OCR | Tesseract | Procesamiento documentos físicos |
| Generación PDF | WeasyPrint (Python) | Plantillas HTML/CSS |
| Email | SendGrid API | Tier gratuito: 100 emails/día |
| WhatsApp | Twilio WhatsApp API | Notificaciones y agente conversacional |
| Scraper | Playwright (Python) | Monitor de fuentes oficiales |
| Streaming | Server-Sent Events (SSE) | Frontend recibe tokens en tiempo real |

---

## 3. Fases de Implementación

---

## FASE 1 — Quick Wins: Optimización Inmediata
**Duración:** Semanas 1–2

> **Objetivo:** Mejorar la experiencia del usuario y reducir tokens sin cambios arquitectónicos mayores. Estas mejoras tienen alto impacto y bajo costo de implementación.

---

### REQ-01 — Streaming de Respuesta

| Campo | Detalle |
|---|---|
| **ID** | REQ-01 |
| **Módulo** | Motor de respuesta |
| **Prioridad** | CRÍTICA |
| **Esfuerzo estimado** | 4–6 horas |

#### Descripción
El sistema debe transmitir la respuesta de Claude token a token en tiempo real, en lugar de esperar la respuesta completa antes de mostrarla al usuario.

#### Requerimientos Funcionales
- El backend debe usar el modo streaming de la API de Anthropic (`messages.stream`)
- Los tokens deben transmitirse al frontend mediante Server-Sent Events (SSE)
- El frontend debe renderizar cada token a medida que llega, sin esperar el mensaje completo
- El cursor de escritura debe ser visible mientras Claude genera la respuesta

#### Requerimientos Técnicos
- Backend Python: reemplazar `messages.create()` por `messages.stream()` con context manager
- Endpoint SSE: `Content-Type: text/event-stream`, `Cache-Control: no-cache`
- Frontend JS: `EventSource` API para recibir el stream y appendear al DOM

#### Criterios de Aceptación
- El usuario ve el primer token en menos de 500ms desde el envío de la consulta
- La respuesta se escribe visualmente de forma progresiva sin saltos
- No hay regresión en la calidad de las respuestas

---

### REQ-02 — Indicadores de Progreso por Etapa

| Campo | Detalle |
|---|---|
| **ID** | REQ-02 |
| **Módulo** | UI / Frontend |
| **Prioridad** | ALTA |
| **Esfuerzo estimado** | 2–3 horas |

#### Descripción
Mientras el sistema procesa la consulta, el usuario debe ver en qué etapa se encuentra el procesamiento.

#### Etapas a Mostrar
- ⏳ Buscando en memoria conversacional...
- ⏳ Consultando base normativa...
- ⏳ Generando respuesta...

#### Requerimientos Funcionales
- Cada etapa debe activarse en el momento exacto en que comienza su procesamiento
- Las etapas completadas deben marcarse visualmente (check o cambio de color)
- Los indicadores deben desaparecer al mostrarse la respuesta completa

#### Criterios de Aceptación
- Los indicadores se sincronizan con el proceso real, no son timers simulados
- Compatible con modo streaming (REQ-01)

---

### REQ-03 — Optimización del System Prompt

| Campo | Detalle |
|---|---|
| **ID** | REQ-03 |
| **Módulo** | Motor RAG / Prompts |
| **Prioridad** | ALTA |
| **Esfuerzo estimado** | 1–2 horas |

#### Descripción
El system prompt actual debe auditarse y optimizarse. Cada token del system prompt se cobra en cada llamada a la API.

#### Requerimientos Funcionales
- Reducir el system prompt a menos de 150 tokens por sector
- Mantener instrucciones esenciales: rol, tono, formato de respuesta, obligación de citar fuentes
- Crear un system prompt específico por sector (aduanas, laboral, tributario, etc.)
- Documentar el system prompt final de cada sector en un archivo de configuración YAML

#### Criterios de Aceptación
- Ningún system prompt supera 150 tokens (medible con tokenizer de Anthropic)
- La calidad de respuestas no decrece respecto a la versión anterior (validar con set de pruebas)

---

### REQ-04 — Límite de Fragmentos RAG

| Campo | Detalle |
|---|---|
| **ID** | REQ-04 |
| **Módulo** | Motor RAG / ChromaDB |
| **Prioridad** | ALTA |
| **Esfuerzo estimado** | 1 hora |

#### Descripción
Limitar el número de fragmentos recuperados de ChromaDB para reducir tokens de entrada y mejorar velocidad.

#### Requerimientos Funcionales
- El parámetro `n_results` de ChromaDB debe configurarse en 3 (valor por defecto actual: 10)
- El valor debe ser configurable via variable de entorno `RAG_MAX_RESULTS`
- Implementar extracción de párrafos relevantes: de cada fragmento, enviar solo los 2 párrafos más similares a la pregunta

#### Criterios de Aceptación
- Reducción medible de tokens de entrada en al menos 40% respecto al valor actual
- Precisión de respuestas no decrece en el set de pruebas

---

## FASE 2 — Memoria y Caché: Experiencia Continua
**Duración:** Semanas 3–5

> **Objetivo:** Dotar al sistema de memoria conversacional por usuario y caché semántico para reutilizar respuestas previas, mejorando experiencia y reduciendo costos operativos.

---

### REQ-05 — Memoria Persistente por Usuario

| Campo | Detalle |
|---|---|
| **ID** | REQ-05 |
| **Módulo** | Memoria / SQLite |
| **Prioridad** | CRÍTICA |
| **Esfuerzo estimado** | 8–12 horas |
| **Dependencias** | Ninguna |

#### Descripción
El sistema debe recordar el historial de conversaciones de cada usuario y usarlo como contexto en consultas futuras, incluso en sesiones distintas.

#### Caso de Uso Principal
Un usuario consulta el día 1: *"¿Qué dice la norma 225 sobre importación de flores?"*. El día 6 vuelve y pregunta: *"¿Recuerdas que hablamos de la norma 225, qué decía en relación a las compras?"*. El sistema debe responder con continuidad, haciendo referencia al intercambio anterior.

#### Esquema de Base de Datos
Crear tabla `conversation_memory` en SQLite:

```sql
CREATE TABLE conversation_memory (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id     TEXT NOT NULL,
  session_id  TEXT,
  role        TEXT NOT NULL,  -- 'user' o 'assistant'
  content     TEXT NOT NULL,
  sources     TEXT,           -- JSON con normas citadas
  created_at  DATETIME DEFAULT CURRENT_TIMESTAMP
);
```

#### Requerimientos Funcionales
- Al recibir una consulta, recuperar las últimas 20 interacciones del usuario
- Inyectar el historial como mensajes previos en el array `messages` de la API
- Prefixar cada mensaje del usuario con su fecha: `[YYYY-MM-DD]`
- Al completar la respuesta, guardar el nuevo intercambio en la BD
- Cada `user_id` solo accede a su propio historial (aislamiento estricto)

#### Política de Retención
- Historial activo: indefinido por defecto
- Configurable por plan de suscripción (30 días, 90 días, indefinido)
- El usuario puede solicitar borrado de su historial desde la plataforma

#### Criterios de Aceptación
- El agente responde con continuidad cuando el usuario referencia conversaciones anteriores
- El historial de un usuario no es visible para otros usuarios
- El sistema funciona correctamente cuando el historial está vacío (primer uso)

---

### REQ-06 — Caché Semántico

| Campo | Detalle |
|---|---|
| **ID** | REQ-06 |
| **Módulo** | Caché / ChromaDB |
| **Prioridad** | ALTA |
| **Esfuerzo estimado** | 8–10 horas |
| **Dependencias** | ChromaDB operativo |

#### Descripción
Cuando un usuario hace una pregunta semánticamente similar a una ya respondida, el sistema debe devolver la respuesta cacheada sin consultar ChromaDB ni llamar a Claude.

#### Arquitectura
- Crear una nueva colección en ChromaDB llamada `cache_respuestas` (separada de `normas_aduanas`)
- Al recibir una pregunta, calcular su embedding y buscar en `cache_respuestas`
- Si la similitud coseno supera el umbral (0.92), devolver respuesta cacheada
- Si no hay hit, procesar normalmente y guardar en caché al finalizar

#### Estructura del Caché
Cada entrada debe incluir en sus metadatos:

```python
{
    "respuesta": "texto completo de la respuesta",
    "fuentes": "[JSON con normas citadas]",
    "sector": "aduanas",
    "created_at": "timestamp",
    "hit_count": 1,
    "version_normativa": "v1.0"
}
```

#### Parámetros Configurables
- `CACHE_SIMILARITY_THRESHOLD`: umbral de similitud (default: `0.92`)
- `CACHE_TTL_DAYS`: días de vida del caché (default: `30`)

#### Invalidación del Caché
- Expiración automática por TTL (30 días)
- Invalidación por sector: al actualizar documentos de un sector, eliminar entradas de caché de ese sector
- Invalidación por versión normativa: cada entrada lleva `version_normativa`; si la BD cambia, el caché viejo se ignora

#### Criterios de Aceptación
- Hit de caché devuelve respuesta en menos de 500ms
- Preguntas semánticamente equivalentes (no idénticas) activan el caché
- El sistema loguea hits y misses para monitoreo

---

### REQ-07 — Compresión de Historial

| Campo | Detalle |
|---|---|
| **ID** | REQ-07 |
| **Módulo** | Memoria / Optimización tokens |
| **Prioridad** | MEDIA |
| **Esfuerzo estimado** | 4–6 horas |
| **Dependencias** | REQ-05 completado |

#### Descripción
Cuando el historial de un usuario supera las 4 interacciones recientes, el sistema debe comprimir las conversaciones antiguas en un resumen, en lugar de inyectarlas completas.

#### Requerimientos Funcionales
- Definir "recientes" como las últimas 4 interacciones (configurable via `MEMORY_RECENT_TURNS`)
- Las interacciones anteriores se comprimen usando Claude Haiku
- El resumen se genera una vez y se cachea en SQLite para no regenerarlo en cada consulta
- El prompt de compresión debe instruir a Haiku a conservar temas normativos clave y fechas relevantes en máximo 150 palabras

#### Criterios de Aceptación
- El historial inyectado nunca supera 600 tokens (sumando resumen + interacciones recientes)
- La continuidad conversacional se mantiene correctamente con el resumen

---

## FASE 3 — Inteligencia del Sistema
**Duración:** Semanas 6–8

> **Objetivo:** Implementar optimización dinámica de modelos, paralelismo en el procesamiento y sistema completo de versionado normativo con monitoreo automático de fuentes.

---

### REQ-08 — Selección Dinámica de Modelo

| Campo | Detalle |
|---|---|
| **ID** | REQ-08 |
| **Módulo** | Motor RAG / Clasificador |
| **Prioridad** | ALTA |
| **Esfuerzo estimado** | 6–8 horas |

#### Descripción
Antes de enviar una consulta a Claude, el sistema debe clasificar su complejidad y seleccionar el modelo adecuado.

#### Criterios de Clasificación

| Tipo | Indicadores | Modelo |
|---|---|---|
| Simple | ¿qué es?, ¿cuándo?, ¿cuánto?, lista, define, plazo | `claude-haiku-4-5` |
| Complejo | analiza, compara, implica, interpreta, diferencia, cómo afecta | `claude-sonnet-4-20250514` |

#### Requerimientos Funcionales
- El clasificador debe ser una función Python rápida basada en palabras clave (sin llamar a Claude)
- El modelo seleccionado debe quedar registrado en el log de cada consulta
- Variable de entorno `FORCE_MODEL` permite forzar un modelo específico (para debug)

#### Criterios de Aceptación
- Al menos el 40% de las consultas se resuelven con Haiku
- La calidad de respuestas simples con Haiku es equivalente a Sonnet (validar con set de pruebas)

---

### REQ-09 — Paralelismo Asíncrono

| Campo | Detalle |
|---|---|
| **ID** | REQ-09 |
| **Módulo** | Backend / Arquitectura async |
| **Prioridad** | MEDIA |
| **Esfuerzo estimado** | 4–6 horas |
| **Dependencias** | REQ-05, REQ-06 |

#### Descripción
Las operaciones de recuperación de memoria (SQLite) y búsqueda RAG (ChromaDB) deben ejecutarse en paralelo usando `asyncio`.

#### Flujo Actual (secuencial)
```
Recuperar memoria → Buscar ChromaDB → Verificar caché → Llamar Claude
```

#### Flujo Objetivo (paralelo)
```
[Recuperar memoria + Buscar ChromaDB + Verificar caché] → Llamar Claude
```

#### Requerimientos Funcionales
- Usar `asyncio.gather()` para ejecutar las tres operaciones en paralelo
- El backend debe ser completamente asíncrono (FastAPI o equivalente)
- Manejo de errores: si una operación paralela falla, continuar con las restantes y loguear

#### Criterios de Aceptación
- Reducción de 1–2 segundos en tiempo de respuesta total medible en pruebas de carga

---

### REQ-10 — Versionado y Trazabilidad Normativa

| Campo | Detalle |
|---|---|
| **ID** | REQ-10 |
| **Módulo** | Versionado / Monitor de fuentes |
| **Prioridad** | ALTA |
| **Esfuerzo estimado** | 16–20 horas |

#### Descripción
El sistema debe rastrear automáticamente cuando una norma es incorporada, modificada o derogada, y permitir consultas temporales como: *"¿qué normativa cambió la semana pasada?"* o *"¿qué normas sobre flores se modificaron en marzo de 2026?"*.

#### Componente 1: Tabla `normative_changelog` (SQLite)

```sql
CREATE TABLE normative_changelog (
  id                  INTEGER PRIMARY KEY AUTOINCREMENT,
  document_id         TEXT NOT NULL,
  sector              TEXT NOT NULL,
  title               TEXT NOT NULL,
  change_type         TEXT NOT NULL,  -- 'incorporacion', 'modificacion', 'derogacion'
  change_date         DATE NOT NULL,
  detected_date       DATETIME,
  source_url          TEXT,
  summary             TEXT,           -- generado por Claude Haiku
  previous_version_id TEXT,
  is_active           BOOLEAN DEFAULT 1
);
```

#### Componente 2: Metadatos en ChromaDB
Cada documento debe incluir: `change_date`, `change_type`, `version`, `is_active`, `source_url`, `tags`.

#### Componente 3: Monitor de Fuentes Oficiales
- Scraper Playwright que corre diariamente via cron job
- Fuentes mínimas: Aduana Chile, SII, Dirección del Trabajo, BCN
- Detección de cambios por hash MD5 del documento
- Al detectar cambio: actualizar ChromaDB + insertar en changelog + generar resumen con Haiku

#### Componente 4: Resolución de Consultas Temporales
- Detectar intenciones temporales en la pregunta del usuario
- Traducir periodos en lenguaje natural a rangos de fecha: *"la semana pasada"*, *"en octubre"*, *"el mes pasado"*
- Consultar `normative_changelog` filtrado por fecha y opcionalmente por sector o tema

#### Componente 5: Diff Narrativo entre Versiones
- Cuando hay versión anterior disponible, Claude Haiku genera resumen de qué cambió
- Formato: **Cambios principales / Lo que se eliminó / Lo que se incorporó**

#### Criterios de Aceptación
- El sistema responde correctamente a consultas de tipo: *"¿qué cambió la semana pasada?"*
- El monitor detecta y registra cambios en fuentes oficiales dentro de las 24 horas
- El diff narrativo es coherente y útil para un profesional de comercio exterior

---

## FASE 4 — Salidas Múltiples: Expansión de Canales
**Duración:** Semanas 9–10

> **Objetivo:** Permitir que los resultados del agente sean entregados por tres canales adicionales: PDF descargable, correo electrónico y WhatsApp.

---

### REQ-11 — Exportación a PDF

| Campo | Detalle |
|---|---|
| **ID** | REQ-11 |
| **Módulo** | Salidas / PDF |
| **Prioridad** | ALTA |
| **Esfuerzo estimado** | 6–8 horas |
| **Librería** | WeasyPrint (Python) |

#### Descripción
El usuario debe poder exportar cualquier respuesta del agente como informe PDF formal.

#### Contenido del PDF
- Membrete con logo Toxiro / AgentIA
- Fecha y hora de la consulta
- Nombre del usuario
- Sector normativo consultado
- Pregunta realizada
- Respuesta completa del agente
- Lista de normas citadas con links a documentos oficiales
- Pie de página con disclaimer legal

#### Requerimientos Funcionales
- Botón "Exportar PDF" visible en cada respuesta del agente
- El PDF se genera en el backend con WeasyPrint usando una plantilla HTML/CSS
- El archivo se descarga directamente en el navegador del usuario
- Nombre del archivo: `agentia_consulta_YYYYMMDD_HHMMSS.pdf`

#### Criterios de Aceptación
- El PDF generado es legible y profesional en Adobe Reader y navegadores
- El tiempo de generación no supera 3 segundos

---

### REQ-12 — Envío por Correo Electrónico

| Campo | Detalle |
|---|---|
| **ID** | REQ-12 |
| **Módulo** | Salidas / Email |
| **Prioridad** | ALTA |
| **Esfuerzo estimado** | 6–8 horas |
| **Servicio** | SendGrid API |

#### Modalidades
- **Envío inmediato:** botón "Enviarme por email" adjunta la respuesta (texto o PDF) al correo registrado del usuario
- **Alertas automáticas:** cuando el monitor detecta un cambio normativo relevante, envía notificación automática

#### Requerimientos Funcionales
- Integración con SendGrid API para envío transaccional
- Plantilla HTML de email responsive con identidad visual Toxiro
- Para alertas: el usuario configura sectores de interés y frecuencia (inmediato, diario, semanal)
- Manejo de bounces y errores de entrega con logging

#### Criterios de Aceptación
- Email entregado en menos de 60 segundos desde la acción del usuario
- Las alertas automáticas se disparan dentro de las 2 horas de detectado el cambio normativo
- El email no cae en carpeta spam (configurar SPF, DKIM en dominio Toxiro)

---

### REQ-13 — Integración WhatsApp

| Campo | Detalle |
|---|---|
| **ID** | REQ-13 |
| **Módulo** | Salidas / WhatsApp |
| **Prioridad** | ALTA |
| **Esfuerzo estimado** | 10–14 horas |
| **Servicio** | Twilio WhatsApp API o Meta Cloud API |

#### Modalidades
- **Notificaciones salientes:** alertas de cambios normativos al número WhatsApp del usuario
- **Agente conversacional:** el usuario realiza consultas directamente desde WhatsApp

#### Requerimientos Funcionales — Notificaciones
- Al detectar cambio normativo, enviar mensaje estructurado con: norma, tipo de cambio, fecha, link
- El usuario puede responder `STOP` para desuscribirse

#### Requerimientos Funcionales — Agente Conversacional
- Webhook que recibe mensajes entrantes de WhatsApp
- El mensaje se procesa igual que una consulta web: caché → memoria → RAG → Claude
- La respuesta se envía al usuario en menos de 10 segundos
- Si la respuesta es muy larga, dividir en múltiples mensajes u ofrecer link al PDF
- La sesión de WhatsApp debe conectarse a la memoria persistente del usuario (REQ-05)

#### Criterios de Aceptación
- El agente responde consultas por WhatsApp con la misma calidad que la interfaz web
- Las notificaciones se reciben dentro de las 2 horas del cambio detectado
- El webhook maneja correctamente mensajes concurrentes de múltiples usuarios

---

## 7. Requerimientos No Funcionales

| Categoría | Requerimiento | Criterio de Medición |
|---|---|---|
| Rendimiento | Primer token (streaming) | Menos de 500ms desde envío |
| Rendimiento | Hit de caché | Menos de 500ms |
| Rendimiento | Generación PDF | Menos de 3 segundos |
| Costo | Reducción de tokens | 60–70% menos vs sistema actual |
| Seguridad | Aislamiento de datos | `user_id` filtra todos los accesos a BD |
| Disponibilidad | Uptime | 99% mensual en VPS Hostinger |
| Escalabilidad | Usuarios concurrentes | Hasta 50 simultáneos en Fase 1 |
| Mantenibilidad | Configuración | Todos los parámetros via variables de entorno |
| Observabilidad | Logging | Cada consulta loguea: modelo, tokens, caché hit/miss, duración |

---

## 8. Roadmap y Entregables por Fase

| Fase | Semanas | Requerimientos | Entregables |
|---|---|---|---|
| Fase 1 | 1 – 2 | REQ-01, REQ-02, REQ-03, REQ-04 | Streaming activo, system prompts optimizados, RAG con límite de fragmentos |
| Fase 2 | 3 – 5 | REQ-05, REQ-06, REQ-07 | Memoria por usuario, caché semántico, compresión de historial |
| Fase 3 | 6 – 8 | REQ-08, REQ-09, REQ-10 | Modelo dinámico, backend async, monitor normativo |
| Fase 4 | 9 – 10 | REQ-11, REQ-12, REQ-13 | Export PDF, envío email, agente WhatsApp |

---

## 9. Impacto Esperado

| Métrica | Situación Actual | Con Mejoras Implementadas |
|---|---|---|
| Tiempo percibido de respuesta | 4–6 segundos en silencio | Inmediato (streaming visible) |
| Costo operativo tokens | 100% (base) | 30–40% del costo actual |
| Continuidad conversacional | Sesiones aisladas | Memoria persistente entre sesiones |
| Actualización normativa | Manual | Automática cada 24 horas |
| Canales de entrega | Solo interfaz web | Web + PDF + Email + WhatsApp |
| Valor diferencial | Consulta normativa básica | Plataforma normativa inteligente |

---

*ToxiroApps — Documento Confidencial de Uso Interno*
*AgentIA v1.0 — Plan de Mejoras — Mayo 2026*
