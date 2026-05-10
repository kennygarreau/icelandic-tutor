FROM node:20-alpine AS builder
ARG REACT_APP_OTEL_COLLECTOR_URL
ENV REACT_APP_OTEL_COLLECTOR_URL=$REACT_APP_OTEL_COLLECTOR_URL
WORKDIR /app
COPY package.json .
RUN npm install
COPY . .
RUN npm run build

FROM nginx:alpine
COPY --from=builder /app/build /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 80
CMD ["nginx", "-g", "daemon off;"]
