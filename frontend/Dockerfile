FROM node:20 AS builder
ARG VITE_OTEL_COLLECTOR_URL
ENV VITE_OTEL_COLLECTOR_URL=$VITE_OTEL_COLLECTOR_URL
ARG VITE_MAPTILER_KEY
ENV VITE_MAPTILER_KEY=$VITE_MAPTILER_KEY
WORKDIR /app
COPY package.json .
RUN npm install
COPY . .
RUN npm run build

FROM nginx:alpine
COPY --from=builder /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 80
CMD ["nginx", "-g", "daemon off;"]
