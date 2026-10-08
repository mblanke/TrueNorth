# Stage 1: Build
FROM node:22-alpine AS build
WORKDIR /app
COPY package.json package-lock.json* ./
RUN npm ci
COPY . .
RUN npm run build:prod

# Stage 2: Serve with nginx
# 1.25-alpine is Alpine 3.19 (end of life). Runs as the image's unprivileged `nginx`
# user (Trivy DS-0002), so it listens on 8080, not 80, and keeps its pid file in /tmp.
FROM nginx:1.30-alpine
RUN sed -i -e '/^user /d' -e 's,^pid .*,pid /tmp/nginx.pid;,' /etc/nginx/nginx.conf \
    && chown -R nginx:nginx /var/cache/nginx
COPY --from=build /app/dist/truenorth-range-web/browser /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf
USER nginx
EXPOSE 8080
CMD ["nginx", "-g", "daemon off;"]
