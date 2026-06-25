Arquitectura de AgentIA

	Este documento explica cómo y por qué está diseñado AgentIA. No describe el
código (ver el README para qué hace), sino las decisiones de arquitectura,
las alternativas que se evaluaron y los trade-offs que se asumieron.

El objetivo es dejar explícito el razonamiento detrás del sistema: para un
arquitecto, las decisiones valen más que la implementación.








1. El problema

Las agencias de aduanas y comercio exterior en Chile operan sobre normativa
regulatoria fragmentada: leyes, resoluciones y procedimientos repartidos entre
múltiples organismos del Estado, que cambian con el tiempo y que no tienen una
fuente única de consulta.

Hoy un agente resuelve sus dudas googleando, preguntando a un colega, o leyendo
PDFs dispersos. Eso es lento, propenso a error, y depende de información que puede
estar desactualizada.

Una IA genérica (ChatGPT, Google) no resuelve esto: responde con conocimiento
general, sin fuente verificable, y puede alucinar normativa que no existe. En un
contexto regulatorio, una respuesta inventada no es un inconveniente: es un riesgo.

AgentIA resuelve esto con una premisa no negociable: toda respuesta proviene de
normativa oficial real. El sistema no inventa, no deduce, no asume.






2. Decisiones de arquitectura

Cada decisión se documenta como: contexto → decisión → alternativas → por qué →
trade-off.

2.1 Fuentes de verdad

Decisión: La normativa se obtiene exclusivamente de organismos oficiales:
Servicio Nacional de Aduanas, Biblioteca del Congreso Nacional (BCN) y Servicio de
Impuestos Internos (SII).

Por qué: El valor del producto es la confiabilidad de la fuente. Anclar el
sistema a organismos oficiales convierte "respuesta de IA" en "normativa real
trazable". Es la diferencia frente a cualquier alternativa genérica.

Trade-off: Quedamos atados a la disponibilidad y al formato de esas fuentes.
Si una fuente cambia su estructura, hay que adaptar la ingesta. Se asume a cambio
de credibilidad.

2.2 Estrategia de ingesta: carga masiva inicial

Decisión: En lugar de consultar las fuentes en tiempo real por cada pregunta,
se hace una carga masiva inicial de toda la normativa vigente a una fecha de
corte, que se indexa en una base vectorial.

Alternativas evaluadas:

* Scraping en vivo por consulta — frágil, lento, dependiente de que la fuente
esté arriba en ese instante.
* API por consulta — no todas las fuentes exponen API; latencia variable.
* Carga masiva + sincronización periódica — elegida.

Por qué: Desacopla la latencia de respuesta de la disponibilidad de la fuente.
El usuario consulta contra un índice local rápido, no contra un sitio de gobierno
en tiempo real. Permite lanzar antes y con un comportamiento predecible.

Trade-off: El índice puede quedar desactualizado entre sincronizaciones. Se
resuelve en 2.3.

2.3 Actualización: detección de cambios por fecha

Decisión: Un proceso periódico recorre las fuentes y, comparando contra la
fecha/estado de lo ya indexado, detecta normativa nueva, modificada o eliminada,
y actualiza solo lo que cambió.

Alternativas evaluadas:

* Re-descargar e re-indexar todo cada vez — ineficiente y costoso.
* Asumir que cierta normativa "nunca cambia" y no revisarla — descartada: si esa
norma llegara a cambiar, quedaría silenciosamente desactualizada. El riesgo de un
falso negativo en un sistema regulatorio no es aceptable.
* Detección incremental por fecha/timestamp — elegida.

Por qué: Mantiene el índice fresco al menor costo, sin puntos ciegos. Es el
mismo patrón que usa cualquier pipeline de datos serio: sincronización incremental
contra la fuente de verdad.

Trade-off: Depende de que las fuentes expongan fechas/versiones confiables.
Cuando no es así, se asume el costo de revisar ese subconjunto con más frecuencia.

2.4 Almacenamiento y recuperación: base vectorial (pgvector sobre PostgreSQL)

Decisión: La normativa se fragmenta, se convierte en embeddings y se almacena
en una base vectorial usando la extensión pgvector sobre el motor relacional
PostgreSQL para su recuperación semántica.

Por qué: Las preguntas de los usuarios no usan las mismas palabras que la ley.
La búsqueda semántica permite encontrar la norma correcta aunque la pregunta esté
formulada en lenguaje cotidiano. Al consolidar la persistencia vectorial y relacional
(usuarios, logs) en un único motor robusto (PostgreSQL), se simplifica radicalmente
la infraestructura operativa y se garantiza transaccionalidad ACID.

Trade-off: La búsqueda vectorial a gran escala en PostgreSQL exige un mantenimiento
y ajuste cuidadoso de índices (HNSW / IVFFlat) en comparación con soluciones vectoriales
gestionadas. Se asume este costo a favor del control absoluto de los datos, contención
de costos y la reducción de piezas móviles en la arquitectura.

2.5 Multi-tenancy: aislamiento por código, no por infraestructura

Decisión: Cada empresa cliente tiene un código identificador. Todas las
consultas y datos se filtran por ese código sobre una infraestructura compartida.

Alternativas evaluadas:

* Infraestructura separada por cliente (VPS/instancia/BD dedicada) — máximo
aislamiento, máximo costo y complejidad operativa.
* Aislamiento lógico por código sobre infraestructura compartida — elegida.

Por qué: El cliente nunca ve datos de otro: el aislamiento lógico cumple el
requisito real. Separar la infraestructura no elimina el riesgo principal (si la
base cae, cae para todos, esté compartida o no), pero sí multiplica el costo. No se
paga complejidad que no resuelve un problema real.

Cuándo escala: Si un cliente exige separación física por contrato o compliance,
entonces se provisiona infraestructura dedicada para ese cliente. Es una
escalada justificada por requerimiento, no un default.

Trade-off: Asumido y consciente: aislamiento lógico en lugar de físico mientras
el caso de uso no exija lo contrario.

2.6 Prevención de alucinaciones: defensa por capas

Este es el requisito central del producto. No se confía en una sola barrera.

Capa 1 — Fuentes controladas: El modelo solo puede responder a partir de la
normativa indexada desde fuentes oficiales. No tiene acceso a conocimiento externo
para construir la respuesta.

Capa 2 — Restricción en el prompt: Se le prohíbe explícitamente inventar,
asumir o deducir. Si la respuesta no está en el contexto recuperado, debe declarar
que no dispone de esa información. La regla de diseño: no dejar puertas abiertas,
porque el sistema eventualmente sale por ellas.

Capa 3 — Trazabilidad (logs): Cada interacción —pregunta, respuesta, fecha,
hora— queda registrada. Ante cualquier discrepancia, se puede reconstruir
exactamente qué se preguntó y qué se respondió.

Capa 4 — Revisión muestral: Operadores revisan respuestas al azar para validar
el comportamiento general del sistema, no cada respuesta individual.

Decisión consciente — no validar el 100%: Se asume un margen de riesgo
controlado. Validar automáticamente cada afirmación de cada respuesta agrega una
complejidad cuyo costo no se justifica al volumen actual. El enfoque es mitigación
fuerte (capas 1–2) + trazabilidad (capa 3) + monitoreo (capa 4), con investigación
puntual cuando aparece una discrepancia real. Esto es riesgo conocido y gobernado,
no negligencia.

2.7 Expansión multi-sector: relaciones derivadas de la norma

Contexto: La normativa aduanera se cruza con la de otros organismos (salud,
normas eléctricas, sanitarias, etc.). Importar un medicamento involucra Aduanas
y Salud. Indexar más sectores sin control generaría ruido: traer normativa que no
aplica degrada la respuesta.

Decisión: La normativa interna de Aduanas declara con qué otros organismos se
relaciona y en qué ámbito. A partir de ese documento oficial se construye una
tabla de relaciones Aduanas ↔ sector. La recuperación solo trae normativa de
otro sector cuando existe una relación declarada.

Por qué: La relevancia se decide desde la fuente oficial, no desde un criterio
propio. Es válido (viene de la norma), mantenible (cuando Aduanas actualiza sus
cruces, se actualiza la tabla) y escalable (sumar un sector es sumar una relación).

Trade-off: Depende de que la fuente declare correctamente sus relaciones. A
cambio, se evita el problema clásico de un RAG multi-dominio: recuperar contexto
irrelevante.






3. Diagrama de arquitectura

flowchart TD subgraph Fuentes["Fuentes oficiales"] A1[Servicio Nacional de Aduanas] A2[Biblioteca del Congreso BCN] A3[Servicio de Impuestos Internos] end subgraph Ingesta["Ingesta y sincronización"] B1[Carga masiva inicial] B2[Sincronización periódica\ndetección de cambios por fecha] end subgraph Indice["Índice de conocimiento"] C1[(Base vectorial\npgvector)] C2[Tabla de relaciones\nmulti-sector] end subgraph Consulta["Capa de consulta"] D1[Recuperación semántica\nfiltrada por código de cliente] D2[Generación restringida al contexto\nprohibido alucinar] D3[(Log de trazabilidad)] end U[Usuario / Agencia] -->|pregunta| D1 A1 --> B1 & B2 A2 --> B1 & B2 A3 --> B1 & B2 B1 --> C1 B2 --> C1 C2 -.relevancia.-> D1 C1 --> D1 D1 --> D2 D2 -->|respuesta + fuente| U D2 --> D3

