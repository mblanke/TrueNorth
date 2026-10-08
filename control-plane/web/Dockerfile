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
FROM nginx:1.25-alpine
COPY --from=build /app/dist/truenorth-range-web/browser /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf
# Rewrites assets/config.json from TN_KEYCLOAK_* at container start (one image, any
# environment). Runs from the stock /docker-entrypoint.sh.
COPY docker/40-truenorth-runtime-config.sh /docker-entrypoint.d/40-truenorth-runtime-config.sh
RUN chmod 0755 /docker-entrypoint.d/40-truenorth-runtime-config.sh
EXPOSE 80
CMD ["nginx", "-g", "daemon off;"]
