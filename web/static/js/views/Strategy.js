import { defineComponent, ref, computed, onMounted } from 'vue';
import { ElMessage, ElMessageBox } from 'element-plus';
import { api } from '../api.js';
import RuleDrawer from '../components/RuleDrawer.js';

export default defineComponent({
    name: 'Strategy',
    components: { RuleDrawer },
    setup() {
        const tab = ref('delete');
        const rules = ref({ select: [], delete: [] });
        const fields = ref({ select: [], delete: [] });
        const operators = ref([]);
        const loading = ref(false);
        const drawerOpen = ref(false);
        const editing = ref(null);
        const dryrunAllLoading = ref(false);

        const current = computed(() => rules.value[tab.value] || []);
        const counts = computed(() => ({
            select: rules.value.select.length,
            delete: rules.value.delete.length,
            enabled: rules.value.select.filter(r => r.enabled).length + rules.value.delete.filter(r => r.enabled).length,
            hits7: rules.value.select.concat(rules.value.delete).reduce((n, r) => n + (r.hits || 0), 0),
        }));

        async function load() {
            loading.value = true;
            try {
                const [list, meta] = await Promise.all([
                    api.get('/api/rules'),
                    api.get('/api/rules/fields'),
                ]);
                rules.value = { select: list.rules.filter(r => r.kind === 'select'), delete: list.rules.filter(r => r.kind === 'delete') };
                fields.value = { select: meta.select, delete: meta.delete };
                operators.value = meta.operators;
            } catch (e) {
                ElMessage.error(e.message || '加载失败');
            } finally {
                loading.value = false;
            }
        }

        function openNew() {
            editing.value = null;
            drawerOpen.value = true;
        }

        function openEdit(row) {
            editing.value = row;
            drawerOpen.value = true;
        }

        async function toggle(row) {
            try {
                const res = await api.put(`/api/rules/${row.id}`, { enabled: row.enabled });
                row.enabled = res.enabled;
                ElMessage.success(res.enabled ? '已启用（下一轮检查生效）' : '已停用');
            } catch (e) {
                row.enabled = !row.enabled;
                ElMessage.error(e.message || '切换失败');
            }
        }

        async function remove(row) {
            try {
                await ElMessageBox.confirm(`删除规则「${row.name}」？审计日志不会被删除。`, '确认删除', { type: 'warning' });
            } catch (e) {
                return;
            }
            await api.delete(`/api/rules/${row.id}`);
            ElMessage.success('已删除');
            load();
        }

        async function dryrun(row) {
            try {
                const res = await api.post(`/api/rules/${row.id}/dryrun`, {});
                ElMessageBox.alert(
                    `检查 ${res.checked} 个，命中 ${res.hit_count} 个。` +
                    (res.hits.length ? '\n\n' + res.hits.slice(0, 6).map(h => h.name).join('\n') : '\n\n当前没有命中的种子。'),
                    `干跑：${row.name}`, { confirmButtonText: '知道了' },
                );
            } catch (e) {
                ElMessage.error(e.message || '干跑失败');
            }
        }

        async function dryrunAll() {
            dryrunAllLoading.value = true;
            try {
                const res = await api.post('/api/rules/dryrun-all', {});
                const lines = res.rules.map(r => `${r.rule_name}：检查 ${r.checked}，命中 ${r.hit_count}`);
                ElMessageBox.alert(lines.join('\n') || '没有启用的规则。', `全部干跑（共命中 ${res.total_hits} 个）`, { confirmButtonText: '知道了' });
            } catch (e) {
                ElMessage.error(e.message || '干跑失败');
            } finally {
                dryrunAllLoading.value = false;
            }
        }

        onMounted(load);
        return {
            tab, rules, fields, operators, loading, drawerOpen, editing, current, counts,
            dryrunAllLoading, load, openNew, openEdit, toggle, remove, dryrun, dryrunAll,
            onSaved: () => load(),
        };
    },
    template: `
        <div class="sites-page strategy-page" v-loading="loading">
            <div class="sites-header">
                <div>
                    <div class="sites-title">策略中心</div>
                    <div class="sites-sub">
                        规则不绑定站点：按你已有的站点生效，导入默认停用，删除有安全阀。
                    </div>
                </div>
                <div class="strategy-actions">
                    <el-button :loading="dryrunAllLoading" @click="dryrunAll">全部干跑</el-button>
                    <el-button type="primary" @click="openNew">新建规则</el-button>
                </div>
            </div>

            <div class="strategy-health">
                <div class="sh-item"><b>{{ counts.enabled }}</b><span>已启用规则</span></div>
                <div class="sh-item"><b>{{ counts.hits7 }}</b><span>近 7 天命中</span></div>
                <div class="sh-item"><b>{{ counts.select }}</b><span>选种规则</span></div>
                <div class="sh-item"><b>{{ counts.delete }}</b><span>删种规则</span></div>
                <div class="sh-note">KEEP 分类与「保种」标签始终跳过；删种前先汇报站点。</div>
            </div>

            <el-tabs v-model="tab" class="strategy-tabs">
                <el-tab-pane :label="'选种 ' + counts.select" name="select">
                    <el-table :data="rules.select" style="width:100%" empty-text="还没有选种规则">
                        <el-table-column prop="priority" label="优先级" width="80" />
                        <el-table-column label="规则" min-width="200">
                            <template #default="{ row }">
                                <div class="rule-name">{{ row.name }}</div>
                                <div class="rule-desc">{{ row.description }}</div>
                            </template>
                        </el-table-column>
                        <el-table-column prop="summary" label="条件摘要" min-width="320" show-overflow-tooltip />
                        <el-table-column label="状态" width="90">
                            <template #default="{ row }">
                                <el-switch v-model="row.enabled" @change="toggle(row)" />
                            </template>
                        </el-table-column>
                        <el-table-column label="7 天命中" width="90">
                            <template #default="{ row }">{{ row.hits || 0 }}</template>
                        </el-table-column>
                        <el-table-column label="操作" width="190">
                            <template #default="{ row }">
                                <el-button text type="primary" @click="dryrun(row)">干跑</el-button>
                                <el-button text @click="openEdit(row)">编辑</el-button>
                                <el-button text type="danger" @click="remove(row)">删除</el-button>
                            </template>
                        </el-table-column>
                    </el-table>
                </el-tab-pane>
                <el-tab-pane :label="'删种 ' + counts.delete" name="delete">
                    <el-table :data="rules.delete" style="width:100%" empty-text="还没有删种规则">
                        <el-table-column prop="priority" label="优先级" width="80" />
                        <el-table-column label="规则" min-width="200">
                            <template #default="{ row }">
                                <div class="rule-name">{{ row.name }}</div>
                                <div class="rule-desc">{{ row.description }}</div>
                            </template>
                        </el-table-column>
                        <el-table-column prop="summary" label="条件摘要" min-width="320" show-overflow-tooltip />
                        <el-table-column label="安全阀" width="120">
                            <template #default="{ row }">
                                <span class="rule-limit">{{ row.limits?.per_round || 3 }} / 轮 · {{ row.limits?.per_hour || 30 }} / 时</span>
                            </template>
                        </el-table-column>
                        <el-table-column label="状态" width="90">
                            <template #default="{ row }">
                                <el-switch v-model="row.enabled" @change="toggle(row)" />
                            </template>
                        </el-table-column>
                        <el-table-column label="7 天命中" width="90">
                            <template #default="{ row }">{{ row.hits || 0 }}</template>
                        </el-table-column>
                        <el-table-column label="操作" width="190">
                            <template #default="{ row }">
                                <el-button text type="primary" @click="dryrun(row)">干跑</el-button>
                                <el-button text @click="openEdit(row)">编辑</el-button>
                                <el-button text type="danger" @click="remove(row)">删除</el-button>
                            </template>
                        </el-table-column>
                    </el-table>
                </el-tab-pane>
            </el-tabs>

            <RuleDrawer v-model="drawerOpen" :rule="editing" :kind="tab" :fields="fields"
                :operators="operators" @saved="onSaved" />
        </div>
    `,
});
