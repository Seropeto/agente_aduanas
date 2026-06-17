# Assets de la Vitrina

Esta carpeta contiene el material visual del Showcase.

## Pendiente de capturar manualmente (requiere instancia en ejecución)

Las siguientes capturas deben tomarse desde una instancia **local de demostración**
(`docker compose -f ../docker-compose.demo.yml up`) — **no desde producción**:

| Archivo esperado | Qué debe mostrar |
|---|---|
| `swagger-overview.png` | Vista general de Swagger UI en `/docs` con la lista de endpoints |
| `swagger-chat-stream.png` | Detalle del endpoint `POST /api/chat/stream` expandido |
| `swagger-auth.png` | Detalle del endpoint de autenticación `POST /api/auth/login` |
| `interfaz-chat.gif` _(opcional)_ | GIF corto del chat respondiendo una consulta de ejemplo |
| `diagrama-procesos.png` _(opcional)_ | Versión renderizada del diagrama de [`../docs/diagrama-procesos.md`](../docs/diagrama-procesos.md) |

## Reglas

- **Sin datos reales**: las capturas no deben mostrar credenciales, dominios de
  producción, ni datos de operaciones/clientes reales.
- Usar datos de ejemplo (la consulta del dron industrial a US$ 45.000 es un buen caso
  demostrativo).
