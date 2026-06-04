# Wiring Stingray Tickets into an existing Traefik setup

This app is designed to sit behind an existing Traefik reverse proxy on your homelab. The
**frontend** container (nginx, port 3000) is the only thing Traefik needs to route to — it
serves the SPA and proxies `/api/*` to the backend over the internal Docker network. The
backend is **not** exposed publicly.

## 1. Join Traefik's network

Traefik discovers containers on a shared Docker network (commonly called `web`, `proxy`, or
`traefik`). In `docker-compose.yml`:

- Uncomment the `web` entries under `frontend.networks` and the top-level `networks:` block,
  and set the name to match your actual Traefik network:

```yaml
  frontend:
    networks:
      - internal
      - web

networks:
  internal:
    driver: bridge
  web:
    external: true        # the network Traefik already runs on
```

Keep `backend` on `internal` only — it should not be reachable from Traefik directly.

You can also drop the `ports: ["3000:3000"]` mapping on the frontend once Traefik routes to
it; Traefik reaches it over the `web` network by container port.

## 2. Add labels to the `frontend` service

Paste this under `frontend.labels` in `docker-compose.yml`. Replace `tickets.example.com`
with your hostname and `websecure` / `myresolver` with your entrypoint and cert resolver
names.

```yaml
    labels:
      - "traefik.enable=true"
      # Router
      - "traefik.http.routers.stingray.rule=Host(`tickets.example.com`)"
      - "traefik.http.routers.stingray.entrypoints=websecure"
      - "traefik.http.routers.stingray.tls=true"
      - "traefik.http.routers.stingray.tls.certresolver=myresolver"
      # Which container port Traefik should forward to
      - "traefik.http.services.stingray.loadbalancer.server.port=3000"
      # Tell Traefik which network to use (only needed if the container is on
      # more than one network)
      - "traefik.docker.network=web"
```

### Optional: HTTP → HTTPS redirect

If you don't already have a global redirect, add a companion router on the web entrypoint:

```yaml
      - "traefik.http.routers.stingray-http.rule=Host(`tickets.example.com`)"
      - "traefik.http.routers.stingray-http.entrypoints=web"
      - "traefik.http.routers.stingray-http.middlewares=stingray-redirect"
      - "traefik.http.middlewares.stingray-redirect.redirectscheme.scheme=https"
```

## 3. Set `COOKIE_SECURE=true`

Once you're serving over HTTPS, set `COOKIE_SECURE=true` in `.env` so the session cookie is
flagged `Secure`. (Leave it `false` for plain-HTTP local testing, or browsers will refuse to
store the cookie.)

## 4. Routing the API separately (optional)

You normally do **not** need a separate route for the API — the nginx frontend already
proxies `/api/*` to the backend internally. Only expose the backend through Traefik if you
want to hit the REST API on its own hostname/path. If you do, add `backend` to the `web`
network and give it its own router (e.g. `Host(...) && PathPrefix(\`/api\`)` with a
strip-prefix middleware), pointing at `server.port=8000`.

## Summary

| Service  | Network(s)       | Traefik label `enable` | Port |
|----------|------------------|------------------------|------|
| frontend | internal + web   | true                   | 3000 |
| backend  | internal         | (none)                 | 8000 |
