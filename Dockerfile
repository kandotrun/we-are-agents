# syntax=docker/dockerfile:1
# Download on the builder architecture; select the binary for the target image.
FROM --platform=$BUILDPLATFORM python:3.13-alpine@sha256:9ef1a599f863231c3d91e941389bfdc4a2d6070557243d54df1a4e57015b960e AS fetch
ARG TARGETARCH
WORKDIR /build
COPY upstream.lock.json ./
COPY waa/__init__.py waa/bootstrap.py ./waa/
COPY scripts/fetch_relay.py ./scripts/
RUN PYTHONPATH=. python scripts/fetch_relay.py --arch "$TARGETARCH" --output /out/iroh-relay

FROM scratch
COPY --from=fetch /out/iroh-relay /usr/local/bin/iroh-relay
COPY examples/docker-relay.toml /etc/we-are-agents/relay.toml
USER 65532:65532
EXPOSE 3340/tcp
ENTRYPOINT ["/usr/local/bin/iroh-relay"]
CMD ["--config-path", "/etc/we-are-agents/relay.toml"]
