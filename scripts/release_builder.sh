#!/usr/bin/env bash
# 仅供发布构建入口 source；独立固定 BuildKit 执行文件时间戳重写，不改变用户默认 builder。
release_builder=""
release_builder_created=0

start_release_builder() {
  release_builder="sms-release-$(date +%s)-$$"
  docker buildx create --driver docker-container --name "$release_builder" \
    --driver-opt image=moby/buildkit:buildx-stable-1@sha256:cec9f139f45e93c5c69c60f8b07cfad9f43f4ef6b6a6cd917527fea5ff2e3dea \
    >/dev/null
  release_builder_created=1
  docker buildx inspect --bootstrap "$release_builder" >/dev/null
}

stop_release_builder() {
  if [ "$release_builder_created" -eq 1 ]; then
    docker buildx rm --force "$release_builder" >/dev/null 2>&1 || true
  fi
}
