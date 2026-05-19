FROM golang:1.25 AS build

WORKDIR /src

COPY go.mod ./
RUN go mod download

COPY . .
RUN CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -o /out/factorio-control-api .

FROM alpine:3.20

WORKDIR /

COPY --from=build /out/factorio-control-api /factorio-control-api

ENTRYPOINT ["/factorio-control-api"]
