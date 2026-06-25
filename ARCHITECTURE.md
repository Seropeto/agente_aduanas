# Arquitectura de AgentIA

> **Documentación Arquitectónica**
> Este documento explica **cómo y por qué** está diseñado AgentIA. No describe el código (ver el README para *qué hace*), sino las **decisiones de arquitectura**, las alternativas que se evaluaron y los trade-offs que se asumieron.
> 
> El objetivo es dejar explícito el razonamiento detrás del sistema: para un arquitecto o auditor técnico, las decisiones estructuradas valen más que la implementación misma.

---

## 1. El problema

Las agencias de aduanas y comercio exterior en Chile operan sobre **normativa regulatoria fragmentada**: leyes, resoluciones y procedimientos repartidos entre múltiples organismos del Estado, que cambian con el tiempo y que no tienen una fuente única de consulta.

Hoy un agente resuelve sus dudas googleando, preguntando a un colega, o leyendo PDFs dispersos. Eso es lento, propenso a error, y depende de información que puede estar desactualizada.

Una IA genérica (ChatGPT, Google) **no resuelve esto**: responde con conocimiento general, sin fuente verificable, y puede alucinar normativa que no existe. En un contexto regulatorio, una respuesta inventada no es un inconveniente: es un riesgo crítico.

**AgentIA resuelve esto con una premisa no negociable:** toda respuesta proviene de normativa oficial real. El sistema no inventa, no deduce, no asume.

---

## 2. Decisiones de arquitectura

Cada decisión se documenta bajo el modelo: *contexto → decisión → alternativas → por qué → trade-off*.

### 2.1 Fuentes de verdad

**Decisión:** La normativa se obtiene exclusivamente de organismos oficiales: Servicio Nacional de Aduanas, Biblioteca del Congreso Nacional (BCN) y Servicio de Impuestos Internos (SII).

**Por qué:** El valor del producto *es* la confiabilidad de la fuente. Anclar el sistema a organismos oficiales convierte "respuesta de IA" en "normativa real trazable". Es la diferencia frente a cualquier alternativa genérica.

**Trade-off:** Quedamos atados a la disponibilidad y al formato de esas fuentes. Si una fuente cambia su estructura, hay que adaptar la ingesta. Se asume a cambio de credibilidad absoluta.

### 2.2 Estrategia de ingesta: carga masiva inicial

**Decisión:** En lugar de consultar las fuentes en tiempo real por cada pregunta, se hace una **carga masiva inicial** de toda la normativa vigente a una fecha de corte, que se indexa en una base vectorial.

**Alternativas evaluadas:**
*   *Scraping en vivo por consulta:* Frágil, lento, dependiente de que la fuente esté arriba en ese instante.
*   *API por consulta:* No todas las fuentes expuestas tienen API; latencia variable.
*   *Carga masiva + sincronización periódica:* Elegida.

**Por qué:** Desacopla la latencia de respuesta de la disponibilidad de la fuente. El usuario consulta contra un índice local rápido, no contra un sitio de gobierno en tiempo real. Permite operar con un comportamiento determinista y predecible.

### 2.3 Actualización: detección de cambios por fecha

**Decisión:** Un proceso periódico recorre las fuentes y, comparando contra la fecha/estado de lo ya indexado, detecta normativa **nueva, modificada o eliminada**, y actualiza solo lo que cambió.

**Por qué:** Mantiene el índice fresco al menor costo, sin puntos ciegos. Es el mismo patrón que usa cualquier pipeline de datos de grado *Enterprise*: sincronización incremental contra la fuente de verdad.

### 2.4 Almacenamiento y recuperación: pgvector (PostgreSQL)

**Decisión:** La normativa se fragmenta, se convierte en embeddings y se almacena en una base vectorial usando la extensión `pgvector` sobre el motor relacional PostgreSQL.

**Por qué:** Consolidar la persistencia vectorial y relacional (usuarios, logs) en un único motor robusto simplifica radicalmente la infraestructura operativa y garantiza transaccionalidad ACID.

### 2.5 Multi-tenancy: aislamiento lógico

**Decisión:** Cada empresa cliente tiene un código identificador. Todas las consultas y datos se filtran por ese código sobre una infraestructura compartida.

**Por qué:** El cliente nunca ve datos de otro: el aislamiento lógico cumple el requisito real sin multiplicar costos operativos prematuramente. Si un cliente corporativo exige separación física por compliance, se provisiona infraestructura dedicada como *upsell*, no como default.

### 2.6 Prevención de alucinaciones: defensa en profundidad

No se confía en una sola barrera. El sistema opera bajo un modelo de mitigación estricto:

*   **Capa 1 — Fuentes controladas:** El modelo solo puede responder a partir de la normativa indexada.
*   **Capa 2 — Restricción en el prompt:** Prohibición explícita de inventar o deducir. Si no está en el contexto, se declara la falta de información.
*   **Capa 3 — Trazabilidad (logs):** Cada interacción queda registrada inmutablemente.
*   **Capa 4 — Revisión muestral:** Operadores validan el comportamiento general del sistema.

### 2.7 Expansión multi-sector (Tabla de Relaciones)

**Decisión:** La recuperación solo trae normativa de otros sectores (ej. Salud, ISP) cuando existe una relación explícitamente declarada en la normativa aduanera.

**Por qué:** Evita el problema clásico del RAG multi-dominio (recuperar contexto ruidoso o irrelevante). La relevancia la decide la ley, no la IA.

---

## 3. Diagrama de arquitectura

```mermaid
%%{init: {'theme': 'base', 'themeVariables': { 'fontFamily': 'arial', 'lineColor': '#64748b'}}}%%
flowchart TD
    classDef oficial fill:#1e40af,stroke:#1e3a8a,color:#ffffff,rx:8px,ry:8px,stroke-width:2px;
    classDef proceso fill:#0ea5e9,stroke:#0284c7,color:#ffffff,rx:8px,ry:8px,stroke-width:2px;
    classDef bd fill:#10b981,stroke:#047857,color:#ffffff,rx:8px,ry:8px,stroke-width:2px;
    classDef consulta fill:#8b5cf6,stroke:#6d28d9,color:#ffffff,rx:8px,ry:8px,stroke-width:2px;
    classDef usuario fill:#f59e0b,stroke:#b45309,color:#ffffff,rx:20px,ry:20px,stroke-width:3px;

    subgraph Fuentes["🏛️ Fuentes Oficiales (Estado)"]
        A1[Servicio Nacional de Aduanas]:::oficial
        A2[Biblioteca del Congreso BCN]:::oficial
        A3[Servicio de Impuestos Internos]:::oficial
    end

    subgraph Ingesta["⚙️ Pipeline de Ingesta"]
        B1[Carga masiva inicial]:::proceso
        B2[Sync periódica y detección de cambios]:::proceso
    end

    subgraph Indice["🗄️ Índice Vectorial y Conocimiento"]
        C1[(Base Vectorial pgvector)]:::bd
        C2[Tabla de relaciones multi-sector]:::bd
    end

    subgraph Consulta["🧠 Capa Cognitiva RAG"]
        D1[Recuperación semántica filtrada]:::consulta
        D2[Generación restringida / Cero Alucinación]:::consulta
        D3[(Log de Trazabilidad y Auditoría)]:::bd
    end

    U((Usuario / Agencia)):::usuario -->|Consulta Normativa| D1
    
    A1 --> B1 & B2
    A2 --> B1 & B2
    A3 --> B1 & B2
    
    B1 -->|Vectores| C1
    B2 -->|Actualizaciones| C1
    C2 -.Define relevancia.-> D1
    C1 -->|Contexto| D1
    D1 --> D2
    D2 -->|Respuesta + Cita Exacta| U
    D2 -->|Registro de evento| D3
