# =============================================================================
# TrueNorth Range — Nginx Reverse Proxy
# =============================================================================
FROM nginx:1.25-alpine

LABEL maintainer="TrueNorth Range DevOps"
LABEL description="Production reverse proxy for TrueNorth Range platform"

# Remove default config
RUN rm -f /etc/nginx/conf.d/default.conf

# Copy configuration
COPY nginx.conf   /etc/nginx/nginx.conf
COPY conf.d/      /etc/nginx/conf.d/

# Create directories for SSL certs, ACME challenges, and cache
RUN mkdir -p /etc/nginx/ssl \
             /var/www/certbot \
             /var/cache/nginx \
    && chown -R nginx:nginx /var/cache/nginx

# Healthcheck — lightweight probe on port 80
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost/health || exit 1

EXPOSE 80 443

CMD ["nginx", "-g", "daemon off;"]