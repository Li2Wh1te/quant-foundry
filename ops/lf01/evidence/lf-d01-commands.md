# LF-D01 本轮实际测试命令与环境说明

所有命令仅针对开发隔离运行时；没有连接生产或供应商。

源码：`/mnt/data/quant-foundry`。测试PostgreSQL：127.0.0.1:55432、
`quant_foundry_test`，测试按用例创建自己的随机schema并只清理自己的对象。
Python 3.12.2、Arrow 23.0.1、DuckDB 1.4.3、PostgreSQL 17.11。

本环境没有网络依赖安装及Docker CLI。`/mnt/data/lfd01-python`将已下载隔离制品的
准确Python解释器、site-packages及DuckDB wheel接入PYTHONPATH，不替换应用算法。
该一次性包装器不是产品代码，不随交付发送运行时二进制。
旧迁移回归的uv子进程使用同一已安装依赖环境，未跳过迁移或增加测试跳过标记。

```sh
. /mnt/data/lfd01-test-env
cd /mnt/data/quant-foundry/backend
/mnt/data/lfd01-python -m pytest -q --tb=short
# 最终：3301 passed, 1 skipped, 234 subtests passed

/mnt/data/lfd01-python -m pytest tests/test_data_store_*.py -q --tb=short
# 最终：327 passed
```

`lfd01-test-env`仅含隔离测试参数和一次性解释器位置。其中
`QF_ENVIRONMENT=test`、`POSTGRES_TEST_ENABLED=1`、`LF_D01_ALLOW_OVERLAY_TEST=1`。
最后一个开关仅用于声明受限overlay开发机制测试，不能在受支持文件系统验收中设置。

受影响模块及历史迁移另有67通过的定向结果；根目录19项测试以普通非root用户执行。
版本检查按仓库`scripts/release_version.py check`进行，0.3.0一致。
最终容量测试对应`scripts/benchmark_current_store.py`的默认全套B01–B04，
在自己的空测试目录运行、显式传入`--allow-overlay-test`，流式合成输入，不包含付费数据。
基准JSON的environment/limits与11项结果为实际输出，并非预设性能目标。

本轮全量回归与容量测试在同一4CPU/4GiB cgroup中部分并行执行，cgroup还包含本地PostgreSQL。
压缩数据量及时间取决于合成样本；不能外推为所有真实领域的生产吞吐保证。

## 未执行命令

新跨容器执行器`check_current_store_containers.py --with-suite`：未执行。
新增`current-store-acceptance.yml`远程CI：未推送、未执行。
本轮前端测试/构建：未执行。远程push/PR更新/merge：未执行。
生产部署/reset/rebuild/清理：未执行。

后续复验可使用`lf-d01-interfaces.md`中的正常锁定依赖/隔离Compose命令，
不依赖本轮一次性`/mnt/data`环境。
