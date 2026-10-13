ARG API_RUNTIME_IMAGE=alpine
FROM ${API_RUNTIME_IMAGE}

ARG BUILDARCH
WORKDIR /app
RUN apk add --no-cache tzdata
COPY ./${BUILDARCH}/release /app/
VOLUME /app/data

EXPOSE 21114
CMD ["./apimain"]
