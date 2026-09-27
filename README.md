# my-k8s-app

应用源码仓库。GitHub Actions 使用 WSL Ubuntu self-hosted runner 构建镜像、推送 Harbor，并更新 GitOps 部署仓库的镜像 tag。

## GitHub Actions runner

runner 标签必须包含：

    self-hosted,linux,x64,harbor

注册并启动 runner：

    cd ~/actions-runner
    ./config.sh --url https://github.com/OWNER/APP_REPOSITORY --token RUNNER_TOKEN --name wsl-ubuntu --labels self-hosted,linux,x64,harbor --work _work --unattended --replace
    ./run.sh

注册 token 只使用一次。关闭 run.sh 后，workflow 无法领取任务。

## GitHub Secrets

配置以下 repository secrets：

| Secret | 用途 |
| --- | --- |
| HARBOR_REGISTRY | Harbor 地址和端口，不含协议 |
| HARBOR_HOST | Harbor 主机名，用于 no_proxy |
| HARBOR_PROJECT | Harbor 项目名 |
| HARBOR_USERNAME | Harbor 用户名 |
| HARBOR_PASSWORD | Harbor 密码或机器人 Token |
| BUILD_HTTP_PROXY | BuildKit 外网 HTTP 代理 |
| DEPLOY_REPO | OWNER/REPOSITORY 格式的部署仓库 |
| GITOPS_TOKEN | 可写部署仓库 main 的 Token |

真实地址和凭据只放在 Secrets。

## 本地 Git

    git config --global user.name "Your Name"
    git config --global user.email "you@example.com"
    git status
    git add .
    git commit -m "describe the change"
    git push origin main

提交前检查：

    git grep -n -I -e "172." -e "password" -e "token" HEAD
