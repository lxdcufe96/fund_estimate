# 净值望远镜（Fund Lens）

一个适合个人使用的基金盘中估值看板。它不依赖已经失效的 `fundgz.1234567.com.cn`，而是根据最近披露的股票持仓和实时股票行情进行透明估算。

## 估值方法

```text
前十大股票持仓的加权平均涨跌幅 × 最近披露股票仓位
```

页面同时展示最新官方净值、持仓日期、行情覆盖率和可信度。估值不是基金公司最终净值，基金调仓、债券、现金、期货、港股汇率等都会产生误差。

## 请求与缓存策略

- 浏览器每分钟只读取一次批量基金快照，单次最多 100 只
- 用户访问只注册活跃基金，不会每人单独触发实时行情请求
- 后台每 30 秒集中刷新最近 10 分钟仍在浏览的活跃基金
- 全部重仓股合并去重，并按每批 100 只股票快速请求行情
- 东方财富行情异常时自动切换腾讯行情
- API 禁止浏览器和运营商缓存，页面展示真实行情时间
- 季度持仓每天检查一次公开报告日期，接口失败时继续使用旧缓存
- 官方净值和资产配置缓存 6 小时，接口失败时继续使用旧缓存
- 实时行情缓存 20 秒，避免同一时刻重复刷新

这套模式适合约 100 名个人用户共享使用。实际容量仍取决于自选基金的去重数量、服务器网络质量和上游公开接口的限流策略。

公开持仓不是基金经理的实时交易记录。程序只能在基金公司披露季报、半年报或年报后更新持仓，无法在实际调仓当天获知变化。

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

## 阿里云更新部署

```bash
cd ~/fund_estimate
git pull
sudo docker stop fund-lens
sudo docker rm fund-lens
sudo docker build -t fund-lens .
sudo docker run -d --name fund-lens --restart unless-stopped -p 8000:8000 fund-lens
```

本项目使用单个 Uvicorn 进程保存共享快照。不要自行增加多个 worker，否则每个 worker 都会建立独立缓存并重复访问上游。

## 部署到 Render

1. 将本项目上传到 GitHub。
2. 登录 [Render](https://render.com)，选择 **New + → Blueprint**。
3. 连接 GitHub 仓库并选择本项目。
4. Render 会读取 `render.yaml` 自动创建服务。
5. 部署完成后直接打开 Render 提供的网址即可。

免费服务长时间无人访问时可能休眠，第一次打开通常需要等待几十秒。

## API

- `GET /api/health`：健康检查和活跃基金/快照数量
- `GET /api/funds/090007`：查询单只基金
- `GET /api/funds?codes=090007,006122`：批量读取快照，最多 100 只
- `GET /docs`：在线 API 文档

## 数据来源与限制

- 官方净值、资产配置、持仓：东方财富公开页面/移动端公开数据
- 股票行情：东方财富公开行情，腾讯行情自动备用
- 服务使用内存缓存降低请求频率
- 仅限个人学习和观察，请勿用于交易决策或商业数据分发
