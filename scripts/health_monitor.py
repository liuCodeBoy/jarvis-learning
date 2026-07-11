#!/usr/bin/env python3
"""
贾维斯系统 - 生产环境健康检查脚本
用于持续监控系统状态并生成报告

功能:
1. 定期健康检查(可配置频率)
2. 关键指标监控
3. 异常检测与告警
4. 状态报告生成
"""

import math
import argparse
import logging
from logging.handlers import RotatingFileHandler
import os
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

# 配置
PROJECT_DIR = Path(__file__).resolve().parents[1]
JARVIS_CORE_URL = os.getenv(
    'JARVIS_CORE_URL', 'http://127.0.0.1:8000'
).rstrip('/')
PROMETHEUS_URL = os.getenv(
    'PROMETHEUS_URL', 'http://127.0.0.1:9090'
).rstrip('/')
DB_PATH = Path(os.getenv('JARVIS_DB_PATH', 'data/jarvis_learning.db'))
if not DB_PATH.is_absolute():
    DB_PATH = PROJECT_DIR / DB_PATH
HEALTH_LOG_PATH = PROJECT_DIR / 'logs' / 'health_check.log'
HEALTH_LOG_PATH.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
HEALTH_LOG_MAX_BYTES = 10 * 1024 * 1024
HEALTH_LOG_BACKUP_COUNT = 7

PROMETHEUS_QUERIES = {
    'jarvis_core_up': 'max(up{job="jarvis-core"})',
    'request_rate_per_second': (
        'sum(rate(jarvis_http_requests_total[5m])) or vector(0)'
    ),
    'server_error_rate': (
        '(sum(rate(jarvis_http_requests_total{status=~"5.."}[5m])) '
        'or vector(0)) / clamp_min('
        '(sum(rate(jarvis_http_requests_total[5m])) or vector(0)), 0.001)'
    ),
    'http_p95_latency_seconds': (
        'histogram_quantile(0.95, sum by (le) '
        '(rate(jarvis_http_request_duration_seconds_bucket[5m])))'
    ),
    'llm_configured': 'max(jarvis_llm_configured)',
}

# 日志配置
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        RotatingFileHandler(
            HEALTH_LOG_PATH,
            maxBytes=HEALTH_LOG_MAX_BYTES,
            backupCount=HEALTH_LOG_BACKUP_COUNT,
            encoding='utf-8',
        ),
        logging.StreamHandler(sys.stdout)
    ],
)
try:
    HEALTH_LOG_PATH.chmod(0o600)
except OSError:
    pass
logger = logging.getLogger('health-monitor')


class HealthMonitor:
    """生产环境健康监控器"""

    def __init__(self):
        self.check_interval = 60  # 60秒检查间隔
        self.alert_thresholds = {
            'response_time_ms': 1000,
            'error_rate': 0.05,
            'p95_latency_seconds': 5,
            'memory_usage_percent': 90,
            'cpu_usage_percent': 80,
            'disk_usage_percent': 90,
        }
        self.history = []
        logger.info("健康监控器初始化完成")

    def run_continuous_check(self):
        """持续健康检查"""
        logger.info("启动持续健康监控...")
        logger.info(f"检查间隔: {self.check_interval}秒")
        logger.info(f"贾维斯核心URL: {JARVIS_CORE_URL}")

        while True:
            try:
                health_report = self.perform_health_check()
                self.history.append(health_report)

                # 保留最近100次检查记录
                if len(self.history) > 100:
                    self.history = self.history[-100:]

                # 检查告警阈值
                self.check_alerts(health_report)

                # 生成报告
                self.generate_report(health_report)

                time.sleep(self.check_interval)

            except KeyboardInterrupt:
                logger.info("健康监控停止")
                break
            except Exception as e:
                logger.error(f"健康检查异常: {e}", exc_info=True)
                time.sleep(300)  # 异常后等待5分钟

    def perform_health_check(self):
        """执行健康检查"""
        check_time = datetime.now()
        report = {
            'timestamp': check_time.isoformat(),
            'overall_status': 'healthy',
            'components': {},
            'metrics': {},
            'alerts': []
        }

        # 1. 核心服务健康检查
        report['components']['jarvis_core'] = self.check_jarvis_core()

        # 2. Prometheus监控检查
        prometheus = self.check_prometheus()
        report['components']['prometheus'] = prometheus

        # 3. 数据库连接检查
        report['components']['database'] = self.check_database()

        # 4. 关键指标采集
        if prometheus['status'] == 'unhealthy':
            report['components']['metric_collection'] = {
                'status': 'unknown',
                'message': 'Prometheus不可用，已跳过指标采集',
            }
        else:
            metrics, metric_collection = self.collect_key_metrics()
            report['metrics'] = metrics
            report['components']['metric_collection'] = metric_collection

        # 5. 系统资源检查
        report['components']['system_resources'] = self.check_system_resources()

        # 计算整体状态
        non_healthy_components = [
            k for k, v in report['components'].items()
            if v.get('status') != 'healthy'
        ]

        critical_components = {
            name for name in ('jarvis_core', 'database')
            if report['components'][name].get('status') == 'unhealthy'
        }
        critical_components.update(
            name for name, component in report['components'].items()
            if component.get('status') == 'critical'
        )

        if critical_components:
            report['overall_status'] = 'unhealthy'
        elif non_healthy_components:
            report['overall_status'] = 'degraded'

        if non_healthy_components:
            report['alerts'].append(
                f"异常组件: {', '.join(non_healthy_components)}"
            )

        return report

    def check_jarvis_core(self):
        """检查贾维斯核心服务"""
        try:
            start_time = time.time()
            response = requests.get(f"{JARVIS_CORE_URL}/health", timeout=10)
            response_time_ms = (time.time() - start_time) * 1000

            if response.status_code == 200:
                try:
                    health_data = response.json()
                except ValueError:
                    return {
                        'status': 'unhealthy',
                        'message': '健康端点返回了无效JSON',
                        'response_time_ms': response_time_ms,
                    }

                if not isinstance(health_data, dict) or health_data.get(
                    'status'
                ) != 'healthy':
                    return {
                        'status': 'unhealthy',
                        'message': '健康端点未报告healthy状态',
                        'response_time_ms': response_time_ms,
                    }

                # 检查响应时间
                if response_time_ms > self.alert_thresholds['response_time_ms']:
                    return {
                        'status': 'warning',
                        'message': f"响应时间过长: {response_time_ms:.2f}ms",
                        'response_time_ms': response_time_ms
                    }

                return {
                    'status': 'healthy',
                    'message': f"服务正常 (响应时间: {response_time_ms:.2f}ms)",
                    'response_time_ms': response_time_ms,
                    'components': health_data.get('components', {})
                }
            else:
                return {
                    'status': 'unhealthy',
                    'message': f"HTTP错误: {response.status_code}",
                    'response_time_ms': response_time_ms
                }

        except requests.exceptions.Timeout:
            return {
                'status': 'unhealthy',
                'message': "请求超时"
            }
        except requests.exceptions.RequestException as error:
            return {
                'status': 'unhealthy',
                'message': f"连接失败: {error}"
            }

    def check_prometheus(self):
        """检查Prometheus监控"""
        try:
            response = requests.get(f"{PROMETHEUS_URL}/-/healthy", timeout=5)
            if response.status_code != 200:
                return {
                    'status': 'unhealthy',
                    'message': f"Prometheus异常: HTTP {response.status_code}"
                }

            try:
                targets_response = requests.get(
                    f"{PROMETHEUS_URL}/api/v1/targets",
                    timeout=5,
                )
                targets_response.raise_for_status()
                payload = targets_response.json()
                if (
                    not isinstance(payload, dict)
                    or payload.get('status') != 'success'
                ):
                    raise ValueError('Prometheus API未返回success状态')
                targets = payload['data']['activeTargets']
                if not isinstance(targets, list) or not all(
                    isinstance(target, dict) for target in targets
                ):
                    raise ValueError('activeTargets不是列表')
                if not all(
                    isinstance(target.get('labels'), dict)
                    for target in targets
                ):
                    raise ValueError('抓取目标标签格式无效')
            except (requests.exceptions.RequestException, KeyError,
                    TypeError, ValueError) as error:
                return {
                    'status': 'degraded',
                    'message': (
                        'Prometheus可用，但目标状态读取失败: '
                        f'{error}'
                    ),
                }

            up_targets = [
                target for target in targets if target.get('health') == 'up'
            ]
            down_targets = [
                target for target in targets if target.get('health') != 'up'
            ]
            core_targets = [
                target for target in targets
                if target.get('labels', {}).get('job') == 'jarvis-core'
            ]
            core_up = any(
                target.get('health') == 'up' for target in core_targets
            )

            if not core_targets:
                status = 'degraded'
                detail = '未发现jarvis-core抓取目标'
            elif not core_up:
                status = 'degraded'
                detail = 'jarvis-core抓取目标不可用'
            elif down_targets:
                status = 'degraded'
                detail = '部分抓取目标不可用'
            else:
                status = 'healthy'
                detail = '所有抓取目标正常'

            return {
                'status': status,
                'message': (
                    f"{detail} (UP: {len(up_targets)}, "
                    f"DOWN: {len(down_targets)})"
                ),
                'up_targets': len(up_targets),
                'down_targets': len(down_targets),
                'jarvis_core_up': core_up,
            }

        except requests.exceptions.RequestException as error:
            return {
                'status': 'unhealthy',
                'message': f"Prometheus连接失败: {error}"
            }

    def check_database(self):
        """检查数据库连接"""
        try:
            if not DB_PATH.exists():
                return {
                    'status': 'unhealthy',
                    'message': "数据库文件不存在"
                }

            with sqlite3.connect(str(DB_PATH)) as connection:
                cursor = connection.cursor()
                cursor.execute("SELECT 1")

                # 检查关键表
                cursor.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
                tables = [row[0] for row in cursor.fetchall()]

            db_size = DB_PATH.stat().st_size

            return {
                'status': 'healthy',
                'message': f"数据库正常 (大小: {db_size / 1024:.2f}KB)",
                'db_size_kb': db_size / 1024,
                'table_count': len(tables)
            }

        except (OSError, sqlite3.Error) as error:
            return {
                'status': 'unhealthy',
                'message': f"数据库错误: {error}"
            }

    def collect_key_metrics(self):
        """采集关键指标"""
        metrics = {}
        missing_metrics = []
        failed_metrics = []

        for metric_name, query in PROMETHEUS_QUERIES.items():
            try:
                value = self.query_prometheus(query)
                if value is None:
                    missing_metrics.append(metric_name)
                else:
                    metrics[metric_name] = value
            except (requests.exceptions.RequestException, KeyError,
                    TypeError, ValueError) as error:
                failed_metrics.append(metric_name)
                logger.warning("指标查询失败 %s: %s", metric_name, error)

        if failed_metrics:
            detail = f"查询失败: {', '.join(failed_metrics)}"
            if missing_metrics:
                detail += f"; 暂无样本: {', '.join(missing_metrics)}"
            component = {
                'status': 'degraded',
                'message': detail,
            }
        elif missing_metrics:
            component = {
                'status': 'warning',
                'message': f"暂无样本: {', '.join(missing_metrics)}",
            }
        else:
            component = {
                'status': 'healthy',
                'message': f"已采集{len(metrics)}项真实指标",
            }

        return metrics, component

    @staticmethod
    def query_prometheus(query):
        """查询即时指标；无样本返回None，协议错误抛出异常。"""
        response = requests.get(
            f"{PROMETHEUS_URL}/api/v1/query",
            params={'query': query},
            timeout=5,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or payload.get('status') != 'success':
            raise ValueError('Prometheus查询未返回success状态')

        data = payload.get('data')
        if not isinstance(data, dict):
            raise ValueError('Prometheus查询数据格式无效')
        result = data.get('result')
        if not isinstance(result, list):
            raise ValueError('Prometheus查询结果格式无效')
        if not result:
            return None

        if not isinstance(result[0], dict):
            raise ValueError('Prometheus样本格式无效')
        sample = result[0].get('value')
        if not isinstance(sample, list) or len(sample) < 2:
            raise ValueError('Prometheus样本格式无效')
        value = float(sample[1])
        return value if math.isfinite(value) else None

    def check_system_resources(self):
        """检查系统资源"""
        try:
            import psutil

            # CPU使用率
            cpu_percent = psutil.cpu_percent(interval=1)

            # 内存使用率
            memory = psutil.virtual_memory()
            memory_percent = memory.percent

            # 磁盘使用率
            disk = psutil.disk_usage('/')
            disk_percent = (disk.used / disk.total) * 100

            status = 'healthy'
            warnings = []

            # 检查阈值
            if cpu_percent > self.alert_thresholds['cpu_usage_percent']:
                status = 'warning'
                warnings.append(f"CPU使用率过高: {cpu_percent:.1f}%")

            if memory_percent > self.alert_thresholds['memory_usage_percent']:
                status = 'warning'
                warnings.append(f"内存使用率过高: {memory_percent:.1f}%")

            if disk_percent > self.alert_thresholds['disk_usage_percent']:
                status = 'critical'
                warnings.append(f"磁盘空间不足: {disk_percent:.1f}%")

            return {
                'status': status,
                'message': '; '.join(warnings) if warnings else '资源充足',
                'cpu_percent': cpu_percent,
                'memory_percent': memory_percent,
                'disk_percent': disk_percent
            }

        except Exception as e:
            return {
                'status': 'unknown',
                'message': f"资源检查失败: {e}"
            }

    def check_alerts(self, report):
        """检查告警阈值"""
        alerts = []

        # 检查响应时间
        jarvis_core = report['components'].get('jarvis_core', {})
        response_time_ms = jarvis_core.get('response_time_ms', 0)
        if response_time_ms > self.alert_thresholds['response_time_ms']:
            alerts.append(
                f"响应时间告警: {response_time_ms:.2f}ms > "
                f"{self.alert_thresholds['response_time_ms']}ms"
            )

        # 检查应用真实暴露的HTTP和LLM指标
        metrics = report['metrics']
        error_rate = metrics.get('server_error_rate')
        if (
            error_rate is not None
            and error_rate > self.alert_thresholds['error_rate']
        ):
            alerts.append(
                f"服务端错误率告警: {error_rate:.2%} > "
                f"{self.alert_thresholds['error_rate']:.2%}"
            )

        p95_latency = metrics.get('http_p95_latency_seconds')
        if (
            p95_latency is not None
            and p95_latency > self.alert_thresholds['p95_latency_seconds']
        ):
            alerts.append(
                f"P95延迟告警: {p95_latency:.2f}s > "
                f"{self.alert_thresholds['p95_latency_seconds']}s"
            )

        core_up = metrics.get('jarvis_core_up')
        if core_up is not None and core_up < 1:
            alerts.append('Prometheus报告jarvis-core抓取目标不可用')

        llm_configured = metrics.get('llm_configured')
        if llm_configured is not None and llm_configured < 1:
            alerts.append('LLM凭据未配置，智能对话能力不可用')

        # 检查系统资源
        resources = report['components'].get('system_resources', {})
        cpu_percent = resources.get('cpu_percent', 0)
        if cpu_percent > self.alert_thresholds['cpu_usage_percent']:
            alerts.append(
                f"CPU告警: {cpu_percent:.1f}% > "
                f"{self.alert_thresholds['cpu_usage_percent']}%"
            )

        memory_percent = resources.get('memory_percent', 0)
        if memory_percent > self.alert_thresholds['memory_usage_percent']:
            alerts.append(
                f"内存告警: {memory_percent:.1f}% > "
                f"{self.alert_thresholds['memory_usage_percent']}%"
            )

        disk_percent = resources.get('disk_percent', 0)
        if disk_percent > self.alert_thresholds['disk_usage_percent']:
            alerts.append(
                f"磁盘告警: {disk_percent:.1f}% > "
                f"{self.alert_thresholds['disk_usage_percent']}%"
            )

        for alert in alerts:
            if alert not in report['alerts']:
                report['alerts'].append(alert)

        # 输出告警
        if alerts:
            logger.warning("⚠️ 告警触发:")
            for alert in alerts:
                logger.warning(f"  - {alert}")

    def generate_report(self, report):
        """生成健康报告"""
        timestamp = report['timestamp']
        overall_status = report['overall_status']

        # 状态颜色
        status_color = {
            'healthy': '🟢',
            'degraded': '🟡',
            'unhealthy': '🔴',
            'warning': '🟡',
            'critical': '🔴'
        }

        status_icon = status_color.get(overall_status, '⚪')

        logger.info(f"\n{'=' * 60}")
        logger.info(f"{status_icon} 健康检查报告 - {timestamp}")
        logger.info(f"{'=' * 60}")
        logger.info(f"整体状态: {overall_status}")

        # 组件状态
        logger.info("\n组件状态:")
        for component, data in report['components'].items():
            component_icon = status_color.get(data.get('status', 'unknown'), '⚪')
            logger.info(
                f"  {component_icon} {component}: "
                f"{data.get('message', 'unknown')}"
            )

        # 关键指标
        if report['metrics']:
            logger.info("\n关键指标:")
            for metric, value in report['metrics'].items():
                logger.info(f"  📊 {metric}: {value:.3f}")

        # 告警信息
        if report.get('alerts'):
            logger.info("\n告警信息:")
            for alert in report['alerts']:
                logger.info(f"  ⚠️ {alert}")

        logger.info(f"\n{'=' * 60}\n")


def main(argv=None):
    """主入口"""
    parser = argparse.ArgumentParser(
        description="Monitor Jarvis continuously or perform one health check."
    )
    parser.add_argument(
        "--once", action="store_true", help="run one check and exit"
    )
    parser.add_argument(
        "--interval", type=float, default=60,
        help="continuous check interval in seconds (default: 60)",
    )
    args = parser.parse_args(argv)
    if args.interval <= 0:
        parser.error("--interval must be greater than zero")

    logger.info("=" * 60)
    logger.info("贾维斯生产环境健康监控器启动")
    logger.info(f"时间: {datetime.now().isoformat()}")
    logger.info("=" * 60)

    monitor = HealthMonitor()
    monitor.check_interval = args.interval

    try:
        if args.once:
            report = monitor.perform_health_check()
            monitor.check_alerts(report)
            monitor.generate_report(report)
            return 0 if report["overall_status"] == "healthy" else 1
        monitor.run_continuous_check()
    except Exception as e:
        logger.error(f"监控器异常终止: {e}", exc_info=True)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
