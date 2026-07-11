"""
贾维斯自学习系统 - Week 3核心算法实现
PrefixSpan序列模式挖掘 + FP-Growth频繁项集 + FTRL在线学习
"""

import threading
import sqlite3
import json
import time
from typing import Dict, List, Tuple, Optional, Set
from collections import defaultdict, Counter
import math


class PrefixSpan:
    """
    PrefixSpan序列模式挖掘算法
    用于发现用户操作序列模式

    示例:
    输入: [[打开IDE, 运行测试, 查看日志], [打开IDE, 编译代码], ...]
    输出: [打开IDE → 运行测试] (支持度30%, 置信度70%)
    """

    def __init__(self, min_support: float = 0.15, min_confidence: float = 0.70,
                 max_pattern_length: int = 10):
        if not 0 < min_support <= 1:
            raise ValueError("min_support must be in (0, 1]")
        if not 0 <= min_confidence <= 1:
            raise ValueError("min_confidence must be in [0, 1]")
        if not 1 <= max_pattern_length <= 50:
            raise ValueError("max_pattern_length must be in [1, 50]")
        self.min_support = min_support
        self.min_confidence = min_confidence
        self.max_pattern_length = max_pattern_length
        self._lock = threading.RLock()
        self._source_sequences: List[List[str]] = []

    def mine_patterns(self, sequences: List[List[str]]) -> List[Tuple[List[str], float, float]]:
        """
        挖掘序列模式

        Args:
            sequences: 操作序列列表

        Returns:
            List of (pattern, support, confidence)
        """
        with self._lock:
            if not sequences:
                return []

            total_sequences = len(sequences)
            min_support_count = max(1, math.ceil(total_sequences * self.min_support))
            self._source_sequences = sequences

            # Step 1: 找出频繁1-项集
            freq_items = self._find_frequent_items(sequences, min_support_count)

            # Step 2: 递归挖掘序列模式
            patterns = []
            for item in sorted(freq_items):
                # 构建投影数据库
                projected_db = self._build_projected_db(sequences, [item])

                # 递归挖掘
                self._mine_recursive(
                    [item], projected_db, min_support_count, patterns, total_sequences
                )

            # 按支持度排序
            patterns.sort(key=lambda x: x[1], reverse=True)

            return patterns

    def _find_frequent_items(self, sequences: List[List[str]], min_count: int) -> Set[str]:
        """找出频繁1-项集"""
        item_counts = Counter()

        for seq in sequences:
            # 每个序列中每个item只计数一次
            unique_items = set(seq)
            for item in unique_items:
                item_counts[item] += 1

        # 过滤频繁项
        freq_items = {item for item, count in item_counts.items() if count >= min_count}

        return freq_items

    def _build_projected_db(self, sequences: List[List[str]], prefix: List[str]) -> List[List[str]]:
        """构建投影数据库"""
        projected = []

        for seq in sequences:
            # 找到prefix在序列中的位置
            suffix = self._find_suffix(seq, prefix)
            if suffix is not None:
                projected.append(suffix)

        return projected

    def _find_suffix(self, sequence: List[str], prefix: List[str]) -> Optional[List[str]]:
        """找到prefix后的后缀"""
        if not prefix:
            return sequence

        # 查找prefix第一个元素
        first_item = prefix[0]

        try:
            start_idx = sequence.index(first_item)

            # 递归查找剩余prefix
            if len(prefix) > 1:
                remaining_suffix = self._find_suffix(
                    sequence[start_idx + 1:],
                    prefix[1:]
                )
                return remaining_suffix
            else:
                return sequence[start_idx + 1:]

        except ValueError:
            # prefix不在序列中
            return None

    def _mine_recursive(self, prefix: List[str], projected_db: List[List[str]],
                       min_count: int, patterns: List, total_sequences: int):
        """递归挖掘序列模式"""
        if not projected_db:
            return

        # 计算当前prefix的支持度
        support_count = len(projected_db)
        support = support_count / total_sequences

        # 计算置信度(需要前缀的支持度)
        confidence = 1.0  # 对于长度为1的模式,置信度为1
        if len(prefix) > 1:
            # Confidence is support(prefix) / support(prefix without last item).
            prefix_support = self._calculate_prefix_support(prefix[:-1], total_sequences)
            if prefix_support > 0:
                confidence = support / prefix_support

        # 如果满足阈值,添加到结果
        if support_count >= min_count and confidence >= self.min_confidence:
            patterns.append((prefix, support, confidence))

        if support_count >= min_count and len(prefix) < self.max_pattern_length:
            # 继续扩展
            freq_items = self._find_frequent_items(projected_db, min_count)

            for item in sorted(freq_items):
                new_prefix = prefix + [item]
                new_projected_db = self._build_projected_db(projected_db, [item])

                self._mine_recursive(
                    new_prefix, new_projected_db, min_count, patterns, total_sequences
                )

    def _calculate_prefix_support(self, prefix: List[str], total_sequences: int) -> float:
        """Calculate subsequence support against the source sequences."""
        if not prefix or not total_sequences:
            return 0.0
        matches = sum(
            1 for sequence in self._source_sequences
            if self._find_suffix(sequence, prefix) is not None
        )
        return matches / total_sequences

    def extract_operation_patterns(self, db_path: str = "jarvis_learning.db",
                                   time_window_hours: int = 168,
                                   user_id: Optional[str] = None) -> List[Dict]:
        """
        从数据库提取操作模式

        Args:
            db_path: 数据库路径
            time_window_hours: 时间窗口(小时)

        Returns:
            模式列表
        """
        # 从数据库读取操作序列
        conn = sqlite3.connect(db_path)

        if user_id:
            cursor = conn.execute("""
                SELECT i.session_id, i.interaction_type, i.timestamp
                FROM interactions AS i
                JOIN sessions AS s ON s.session_id = i.session_id
                WHERE i.timestamp > ? AND s.user_id = ?
                ORDER BY i.session_id, i.timestamp
            """, (time.time() - time_window_hours * 3600, user_id))
        else:
            cursor = conn.execute("""
                SELECT session_id, interaction_type, timestamp
                FROM interactions
                WHERE timestamp > ?
                ORDER BY session_id, timestamp
            """, (time.time() - time_window_hours * 3600,))

        # 按session分组
        sessions = defaultdict(list)
        for row in cursor.fetchall():
            session_id, interaction_type, timestamp = row
            sessions[session_id].append(interaction_type)

        conn.close()

        # 转换为序列列表
        sequences = list(sessions.values())

        # 挖掘模式
        patterns = self.mine_patterns(sequences)

        # 转换为字典格式
        result = []
        for pattern, support, confidence in patterns:
            result.append({
                'pattern': pattern,
                'pattern_str': ' → '.join(pattern),
                'support': support,
                'confidence': confidence,
                'length': len(pattern)
            })

        return result


class FPGrowth:
    """
    FP-Growth频繁项集挖掘算法
    用于发现共现操作

    示例:
    输入: [[VSCode, Terminal, Chrome], [VSCode, Chrome], ...]
    输出: {VSCode, Chrome} (支持度40%)
    """

    def __init__(self, min_support: float = 0.15):
        self.min_support = min_support
        self._lock = threading.RLock()

    def mine_frequent_itemsets(self, transactions: List[List[str]]) -> List[Tuple[Set[str], float]]:
        """
        挖掘频繁项集

        Args:
            transactions: 事务列表

        Returns:
            List of (itemset, support)
        """
        with self._lock:
            if not transactions:
                return []

            total_transactions = len(transactions)
            min_support_count = max(1, math.ceil(total_transactions * self.min_support))

            # Step 1: 构建FP树(简化实现)
            # 使用Apriori算法作为简化版本

            # 找出所有频繁1-项集
            item_counts = Counter()
            for trans in transactions:
                for item in set(trans):  # 每个事务中每个item只计数一次
                    item_counts[item] += 1

            freq_items = {item for item, count in item_counts.items()
                         if count >= min_support_count}

            # 生成频繁项集
            itemsets = []

            # 1-项集
            for item in freq_items:
                support = item_counts[item] / total_transactions
                itemsets.append(({item}, support))

            # 2-项集(简化:只计算到2-项集)
            freq_items_list = list(freq_items)
            for i in range(len(freq_items_list)):
                for j in range(i + 1, len(freq_items_list)):
                    item1, item2 = freq_items_list[i], freq_items_list[j]

                    # 计算同时出现的次数
                    co_occurrence_count = sum(
                        1 for trans in transactions
                        if item1 in trans and item2 in trans
                    )

                    if co_occurrence_count >= min_support_count:
                        support = co_occurrence_count / total_transactions
                        itemsets.append(({item1, item2}, support))

            # 按支持度排序
            itemsets.sort(key=lambda x: x[1], reverse=True)

            return itemsets

    def extract_co_occurrence_patterns(self, db_path: str = "jarvis_learning.db",
                                       time_window_hours: int = 168,
                                       user_id: Optional[str] = None) -> List[Dict]:
        """
        提取共现操作模式

        Args:
            db_path: 数据库路径
            time_window_hours: 时间窗口(小时)

        Returns:
            共现模式列表
        """
        # 从数据库读取操作
        conn = sqlite3.connect(db_path)

        if user_id:
            cursor = conn.execute("""
                SELECT i.session_id, i.interaction_type
                FROM interactions AS i
                JOIN sessions AS s ON s.session_id = i.session_id
                WHERE i.timestamp > ? AND s.user_id = ?
            """, (time.time() - time_window_hours * 3600, user_id))
        else:
            cursor = conn.execute("""
                SELECT session_id, interaction_type
                FROM interactions
                WHERE timestamp > ?
            """, (time.time() - time_window_hours * 3600,))

        # 按session分组
        sessions = defaultdict(set)
        for row in cursor.fetchall():
            session_id, interaction_type = row
            sessions[session_id].add(interaction_type)

        conn.close()

        # 转换为事务列表
        transactions = [list(items) for items in sessions.values()]

        # 挖掘频繁项集
        itemsets = self.mine_frequent_itemsets(transactions)

        # 转换为字典格式
        result = []
        for itemset, support in itemsets:
            if len(itemset) > 1:  # 只返回多项集
                result.append({
                    'itemset': list(itemset),
                    'itemset_str': ', '.join(sorted(itemset)),
                    'support': support,
                    'size': len(itemset)
                })

        return result


class FTRLOnlineLearning:
    """
    FTRL (Follow-The-Regularized-Leader) 在线学习算法
    用于实时更新用户偏好模型

    特性:
    - 增量更新(<100ms延迟)
    - L1正则化(稀疏性)
    - L2正则化(平滑性)
    """

    def __init__(self,
                 feature_dim: int = 100,
                 alpha: float = 0.01,
                 beta: float = 1.0,
                 lambda1: float = 0.1,
                 lambda2: float = 1.0):

        self.feature_dim = feature_dim
        self.alpha = alpha
        self.beta = beta
        self.lambda1 = lambda1
        self.lambda2 = lambda2

        self._lock = threading.RLock()

        # FTRL参数
        self.z = [0.0] * feature_dim  # 累积梯度
        self.n = [0.0] * feature_dim  # 累积梯度平方

        # 权重(懒惰计算)
        self.w = [0.0] * feature_dim

        # 训练统计
        self._sample_count = 0
        self._last_update_time = 0

    def predict(self, features: List[float]) -> float:
        """
        预测

        Args:
            features: 特征向量(长度=feature_dim)

        Returns:
            预测值(0-1)
        """
        with self._lock:
            if len(features) != self.feature_dim:
                raise ValueError(f"features must contain {self.feature_dim} values")
            # 计算权重(懒惰更新)
            self._update_weights()

            # 计算预测值
            score = sum(w * f for w, f in zip(self.w, features))

            # Sigmoid激活
            score = max(-35.0, min(35.0, score))
            prediction = 1.0 / (1.0 + math.exp(-score))

            return prediction

    def update(self, features: List[float], label: float):
        """
        在线更新模型

        Args:
            features: 特征向量
            label: 标签(0或1)
        """
        with self._lock:
            if len(features) != self.feature_dim:
                raise ValueError(f"features must contain {self.feature_dim} values")
            if label not in (0, 1, 0.0, 1.0):
                raise ValueError("label must be 0 or 1")
            start_time = time.time()

            # 预测当前样本
            prediction = self.predict(features)

            # 计算梯度
            gradient = prediction - label

            # 更新每个特征
            for i in range(self.feature_dim):
                if features[i] != 0:  # 只更新非零特征
                    coordinate_gradient = gradient * features[i]
                    next_n = self.n[i] + coordinate_gradient * coordinate_gradient
                    sigma = (
                        math.sqrt(next_n) - math.sqrt(self.n[i])
                    ) / self.alpha
                    self.z[i] += coordinate_gradient - sigma * self.w[i]
                    self.n[i] = next_n

            # 更新统计
            self._sample_count += 1
            self._last_update_time = time.time()

            # 计算延迟
            latency = (time.time() - start_time) * 1000  # ms

            return latency

    def _update_weights(self):
        """更新权重(懒惰计算)"""
        for i in range(self.feature_dim):
            self.w[i] = self._compute_weight(i)

    def _compute_weight(self, i: int) -> float:
        """计算单个权重"""
        if abs(self.z[i]) <= self.lambda1:
            return 0.0

        # FTRL权重更新公式
        sign = 1 if self.z[i] > 0 else -1

        weight = -1.0 * (self.z[i] - sign * self.lambda1) / (
            (self.beta + math.sqrt(self.n[i])) / self.alpha + self.lambda2
        )

        return weight

    def get_model_info(self) -> Dict:
        """获取模型信息"""
        with self._lock:
            non_zero_weights = sum(1 for w in self.w if abs(w) > 1e-6)

            return {
                'feature_dim': self.feature_dim,
                'sample_count': self._sample_count,
                'non_zero_weights': non_zero_weights,
                'sparsity': 1.0 - non_zero_weights / self.feature_dim,
                'last_update_time': self._last_update_time
            }

    def save_model(self, filepath: str):
        """保存模型"""
        with self._lock:
            model_data = {
                'feature_dim': self.feature_dim,
                'alpha': self.alpha,
                'beta': self.beta,
                'lambda1': self.lambda1,
                'lambda2': self.lambda2,
                'z': self.z,
                'n': self.n,
                'w': self.w,
                'sample_count': self._sample_count,
                'last_update_time': self._last_update_time
            }

            with open(filepath, 'w') as f:
                json.dump(model_data, f)

    def load_model(self, filepath: str):
        """加载模型"""
        with self._lock:
            with open(filepath, 'r') as f:
                model_data = json.load(f)

            self.feature_dim = model_data['feature_dim']
            self.alpha = model_data['alpha']
            self.beta = model_data['beta']
            self.lambda1 = model_data['lambda1']
            self.lambda2 = model_data['lambda2']
            self.z = model_data['z']
            self.n = model_data['n']
            self.w = model_data['w']
            self._sample_count = model_data['sample_count']
            self._last_update_time = model_data['last_update_time']


class HabitLearningProvider:
    """
    习惯学习提供者 - 统一管理三种算法
    """

    def __init__(self, db_path: str, user_id: str):
        if not user_id or not user_id.strip():
            raise ValueError("user_id is required")
        self.db_path = db_path
        self.user_id = user_id.strip()
        self.prefix_span = PrefixSpan(min_support=0.15, min_confidence=0.70)
        self.fp_growth = FPGrowth(min_support=0.15)
        self.ftrl = FTRLOnlineLearning(feature_dim=100)

        self._lock = threading.RLock()

    def learn_operation_patterns(self) -> List[Dict]:
        """学习操作模式"""
        patterns = self.prefix_span.extract_operation_patterns(
            self.db_path, user_id=self.user_id
        )

        # 保存到数据库
        self._save_patterns_to_db(patterns)

        return patterns

    def learn_co_occurrence(self) -> List[Dict]:
        """学习共现模式"""
        itemsets = self.fp_growth.extract_co_occurrence_patterns(
            self.db_path, user_id=self.user_id
        )

        # 保存到数据库
        self._save_itemsets_to_db(itemsets)

        return itemsets

    def update_user_preference(self, features: List[float], feedback: float) -> float:
        """
        更新用户偏好(在线学习)

        Args:
            features: 特征向量
            feedback: 用户反馈(0-1)

        Returns:
            更新延迟(ms)
        """
        latency = self.ftrl.update(features, feedback)
        return latency

    def predict_user_preference(self, features: List[float]) -> float:
        """预测用户偏好"""
        return self.ftrl.predict(features)

    def train_on_new_data(self) -> Dict:
        """在新数据上训练"""
        result = {
            'operation_patterns': 0,
            'co_occurrence': 0,
            'online_updates': 0
        }

        # 学习操作模式
        patterns = self.learn_operation_patterns()
        result['operation_patterns'] = len(patterns)

        # 学习共现模式
        itemsets = self.learn_co_occurrence()
        result['co_occurrence'] = len(itemsets)

        return result

    def _save_patterns_to_db(self, patterns: List[Dict]):
        """保存模式到数据库"""
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE")

        try:
            for pattern in patterns:
                conn.execute("""
                    INSERT INTO operation_patterns
                    (user_id, operation_sequence, sequence_length, support, confidence,
                     pattern_type, first_discovered, last_observed, observation_count)
                    VALUES (?, ?, ?, ?, ?, 'sequence', ?, ?, 1)
                """, (
                    self.user_id,
                    json.dumps(pattern['pattern']),
                    pattern['length'],
                    pattern['support'],
                    pattern['confidence'],
                    time.time(),
                    time.time()
                ))

            conn.commit()

        except Exception:
            conn.rollback()
            raise

        finally:
            conn.close()

    def _save_itemsets_to_db(self, itemsets: List[Dict]):
        """保存项集到数据库"""
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE")

        try:
            for itemset in itemsets:
                conn.execute("""
                    INSERT INTO operation_patterns
                    (user_id, itemset, itemset_frequency, pattern_type,
                     first_discovered, last_observed)
                    VALUES (?, ?, ?, 'itemset', ?, ?)
                """, (
                    self.user_id,
                    json.dumps(itemset['itemset']),
                    int(itemset['support'] * 100),
                    time.time(),
                    time.time()
                ))

            conn.commit()

        except Exception:
            conn.rollback()
            raise

        finally:
            conn.close()

