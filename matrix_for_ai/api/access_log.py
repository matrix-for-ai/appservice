from aiohttp.abc import AbstractAccessLogger


class AccessLoggerWithoutQuery(AbstractAccessLogger):
    def log(self, request, response, time: float) -> None:
        self.logger.info('%s "%s %s" %d %d %.3fs "%s"',
                         request.remote, request.method, request.rel_url.raw_path,
                         response.status, response.body_length, time,
                         request.headers.get("User-Agent", "-"))
