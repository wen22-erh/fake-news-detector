# DOCKER 練習

---

## Dockerfile 架設指令說明

-   **FROM**：Build this image from specified image
-   **WORKDIR**：sets the working directory for all following commands (like changing into a directory cd...)
-   **RUN**：will execute any command in a shell inside the container environment
-   **COPY**：takes 2 arguments, first is package\*.json, second is the place we want to copy it in the container which is ./(the current working directory)
-   **COPY . .**：copy all the source files to our current working directory
-   **CMD ["", ""]**：tell the container how to run the application
-   **.dockerignore**：to put something you don't want to copy to the container

---

## 常用 Docker 指令

| 指令                                    | 說明                                                        |
| --------------------------------------- | ----------------------------------------------------------- |
| `docker ps`                             | show every container you're working in your system          |
| `docker ps -a`                          | show all containers (including stopped ones)                |
| `docker images`                         | show all your images                                        |
| `docker build -t "your project name" .` | build image and tag it with your project name               |
| `docker pull nginx:1.23`                | pull images from dockerhub                                  |
| `docker run -d -p 9000:80 nginx:1.23`   | run in background, map port 9000 on host to 80 in container |

---

## 小提醒

-   使用 `.dockerignore` 可以避免不必要的檔案被複製進 container
-   指定明確且有意義的 image tag
-   如果遇到問題，可以用 `docker logs <container_id>` 查看日誌

---
