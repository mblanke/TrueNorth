# Stage 1: Build
FROM node:22-alpine AS build
WORKDIR /app
COPY package.json package-lock.json* ./
RUN npm ci
COPY . .
# The production configuration swaps in environment.prod.ts (auth on). check:prod-env
# fails the image build if the bundle would ship with authDisabled: true.
RUN npm run build:prod && npm run check:prod-env

# Stage 2: Serve with nginx
# 1.25-alpine is Alpine 3.19 (end of life). Runs as the image's unprivileged `nginx`
# user (Trivy DS-0002), so it listens on 8080, not 80, and keeps its pid file in /tmp.
FROM nginx:1.30-alpine
RUN sed -i -e '/^user /d' -e 's,^pid .*,pid /tmp/nginx.pid;,' /etc/nginx/nginx.conf \
    && chown -R nginx:nginx /var/cache/nginx
COPY --from=build /app/dist/truenorth-range-web/browser /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf
# Writes the SPA's runtime config from TN_* env at container start (one image, any
# environment). Runs from the stock /docker-entrypoint.sh, as the `nginx` user, so it
# writes under /tmp/truenorth-runtime; nginx.conf serves /assets/config.json from there
# and falls back to the built-in copy. The web root stays root-owned and read-only.
COPY docker/40-truenorth-runtime-config.sh /docker-entrypoint.d/40-truenorth-runtime-config.sh
RUN chmod 0755 /docker-entrypoint.d/40-truenorth-runtime-config.sh
USER nginx
EXPOSE 8080
CMD ["nginx", "-g", "daemon off;"]
