# 在官方镜像上叠加改后的代码：
# 基础镜像已带 python 运行时、gosu、entrypoint 和全部依赖，
# 这里只替换 /app 下的源码，避免依赖版本漂移。
FROM xinya585/ptbrush:latest

# 基础镜像里带着旧版 /app，直接 COPY 只会覆盖同名文件、不会删掉已下线的文件，
# 所以先清空再铺，保证镜像里的 /app 与本源码目录完全一致。
# 注意 /app/data 是基础镜像声明的 VOLUME（构建时挂着、删不掉），要排除在外。
RUN find /app -mindepth 1 -maxdepth 1 ! -name data -exec rm -rf {} +

COPY ./ /app

WORKDIR /app
