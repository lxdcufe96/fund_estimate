# 净值望远镜（Fund Lens）

一个适合个人使用的基金盘中估值看板。它不依赖已经失效的 `fundgz.1234567.com.cn`，而是根据最近披露的股票持仓和实时股票行情进行透明估算。

## 估值方法

```text
前十大股票持仓的加权平均涨跌幅 × 最近披露股票仓位
```

页面同时展示最新官方净值、持仓日期、行情覆盖率和可信度。估值不是基金公司最终净值，基金调仓、债券、现金、期货、港股汇率等都会产生误差。

## 本地运行

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

打开 <http://127.0.0.1:8000>。

## Docker 运行

```bash
docker build -t fund-lens .
docker run --rm -p 8000:8000 fund-lens
```

## 部署到 Render

1. 将本项目上传到 GitHub。
2. 登录 [Render](https://render.com)，选择 **New + → Blueprint**。
3. 连接 GitHub 仓库并选择本项目。
4. Render 会读取 `render.yaml` 自动创建服务。
5. 部署完成后直接打开 Render 提供的网址即可。

免费服务长时间无人访问时可能休眠，第一次打开通常需要等待几十秒。

## API

- `GET /api/health`：健康检查
- `GET /api/funds/090007`：查询单只基金
- `GET /api/funds?codes=090007,006122`：批量查询，最多 10 只
- `GET /docs`：在线 API 文档

## 数据来源与限制

- 官方净值、资产配置、持仓：东方财富公开页面/移动端公开数据
- 股票行情：东方财富公开行情
- 服务使用内存缓存降低请求频率
- 仅限个人学习和观察，请勿用于交易决策或商业数据分发

