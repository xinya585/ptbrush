import { defineComponent, ref, computed, watch } from 'vue';
import { ElMessage } from 'element-plus';
import { api } from '../api.js';

function blank(kind) {
    return {
        id: 0, kind, name: '', enabled: false, priority: 10, match: 'all',
        description: '', conditions: [{ field: kind === 'select' ? 'promo' : 'ratio', op: kind === 'select' ? 'in' : 'lt', value: kind === 'select' ? 'free,free_2x' : '0.5' }],
        limits: { per_round: 3, per_hour: 30 },
    };
}

export default defineComponent({
    name: 'RuleDrawer',
    props: {
        modelValue: Boolean,
        rule: Object,
        kind: String,
        fields: { type: Object, default: () => ({ select: [], delete: [] }) },
        operators: { type: Array, default: () => [] },
    },
    emits: ['update:modelValue', 'saved'],
    setup(props, { emit }) {
        const form = ref(blank(props.kind || 'delete'));
        const saving = ref(false);
        const dryrunLoading = ref(false);
        const dryrun = ref(null);

        watch(() => props.modelValue, (open) => {
            if (open) {
                form.value = props.rule ? JSON.parse(JSON.stringify(props.rule)) : blank(props.kind || 'delete');
                if (form.value.kind === 'delete' && !form.value.limits) form.value.limits = { per_round: 3, per_hour: 30 };
                if (!form.value.conditions?.length) form.value.conditions = blank(form.value.kind).conditions;
                dryrun.value = null;
            }
        });

        const fieldOptions = computed(() => (form.value.kind === 'select' ? props.fields.select : props.fields.delete) || []);
        const fieldLabel = (key) => (fieldOptions.value.find(f => f.key === key) || {}).label || key;
        const opLabel = (key) => (props.operators.find(o => o.key === key) || {}).label || key;

        const summaryText = computed(() => {
            const joiner = form.value.match === 'any' ? ' 或 ' : ' 且 ';
            const parts = (form.value.conditions || []).map(c =>
                `${fieldLabel(c.field)} ${opLabel(c.op)} ${Array.isArray(c.value) ? c.value.join(' / ') : (c.value ?? '')}`
            );
            return parts.length ? parts.join(joiner) : '（未设置条件）';
        });

        function addCondition() {
            form.value.conditions.push({
                field: form.value.kind === 'select' ? 'size' : 'ratio',
                op: form.value.kind === 'select' ? 'gte' : 'lt',
                value: '',
            });
        }

        function removeCondition(i) {
            form.value.conditions.splice(i, 1);
        }

        function normalizeValue(c) {
            if (c.op === 'in' || c.op === 'not_in') {
                return Array.isArray(c.value) ? c.value : String(c.value || '').split(',').map(s => s.trim()).filter(Boolean);
            }
            return c.value;
        }

        function payload() {
            return {
                kind: form.value.kind,
                name: String(form.value.name || '').trim(),
                enabled: !!form.value.enabled,
                priority: Number(form.value.priority || 10),
                match: form.value.match,
                description: form.value.description || '',
                conditions: (form.value.conditions || []).map(c => ({
                    field: c.field, op: c.op, value: normalizeValue(c),
                    value2: c.value2,
                })),
                limits: form.value.limits || {},
            };
        }

        async function save() {
            if (!payload().name) return ElMessage.warning('先给规则起个名字');
            if (!payload().conditions.length) return ElMessage.warning('至少保留一条条件');
            saving.value = true;
            try {
                const body = payload();
                let res;
                if (form.value.id) res = await api.put(`/api/rules/${form.value.id}`, body);
                else res = await api.post('/api/rules', body);
                ElMessage.success('已保存');
                emit('saved', res);
                emit('update:modelValue', false);
            } catch (e) {
                ElMessage.error(e.message || '保存失败');
            } finally {
                saving.value = false;
            }
        }

        async function runDryrun() {
            if (!form.value.id) return ElMessage.warning('先保存规则，再干跑');
            dryrunLoading.value = true;
            try {
                dryrun.value = await api.post(`/api/rules/${form.value.id}/dryrun`, {});
                ElMessage.success(`干跑完成：检查 ${dryrun.value.checked} 个，命中 ${dryrun.value.hit_count} 个`);
            } catch (e) {
                ElMessage.error(e.message || '干跑失败');
            } finally {
                dryrunLoading.value = false;
            }
        }

        return {
            form, saving, dryrunLoading, dryrun, fieldOptions, summaryText,
            addCondition, removeCondition, save, runDryrun, opLabel,
            close: () => emit('update:modelValue', false),
            updateVisible: (v) => emit('update:modelValue', v),
        };
    },
    template: `
        <el-drawer :model-value="modelValue" @update:model-value="updateVisible"
            :title="form.id ? '编辑规则' : '新建规则'" size="560px" class="rule-drawer">
            <div class="rd-body">
                <div class="rd-row">
                    <el-input v-model="form.name" placeholder="规则名，例如：低分享率 · 做种 6h 后" />
                    <el-select v-model="form.priority" style="width:110px;margin-left:8px;">
                        <el-option v-for="n in [5,10,20,30]" :key="n" :label="'优先级 ' + n" :value="n" />
                    </el-select>
                </div>
                <div class="rd-tip">
                    {{ form.kind === 'select' ? '选种规则：按优先级尝试，命中即视为候选；不启用则完全不参与。' : '删种规则：按优先级匹配，第一条命中的规则执行删除。' }}
                </div>

                <div class="rd-label">匹配方式</div>
                <el-radio-group v-model="form.match">
                    <el-radio-button value="all">全部满足</el-radio-button>
                    <el-radio-button value="any">任一满足</el-radio-button>
                </el-radio-group>

                <div class="rd-label">条件</div>
                <div class="rd-cond" v-for="(c, i) in form.conditions" :key="i">
                    <el-select v-model="c.field" style="width:140px;" filterable>
                        <el-option v-for="f in fieldOptions" :key="f.key" :label="f.label" :value="f.key" />
                    </el-select>
                    <el-select v-model="c.op" style="width:110px;margin:0 6px;">
                        <el-option v-for="o in operators" :key="o.key" :label="o.label" :value="o.key" />
                    </el-select>
                    <el-input v-model="c.value" style="flex:1;" :placeholder="c.op === 'in' ? '逗号分隔，如 free,free_2x' : '如 10 GiB / 200 KiB/s / 6 h'" />
                    <el-input v-if="c.op === 'between'" v-model="c.value2" style="width:110px;margin-left:6px;" placeholder="上限" />
                    <el-button text type="danger" @click="removeCondition(i)" style="margin-left:4px;">删</el-button>
                </div>
                <el-button text type="primary" @click="addCondition">+ 添加条件</el-button>

                <div class="rd-label">一句话摘要</div>
                <div class="rd-summary">{{ summaryText }}</div>

                <div v-if="form.kind === 'delete'">
                    <div class="rd-label">安全阀</div>
                    <div class="rd-row">
                        <el-input-number v-model="form.limits.per_round" :min="1" :max="50" />
                        <span class="rd-unit">个 / 轮</span>
                        <el-input-number v-model="form.limits.per_hour" :min="1" :max="200" style="margin-left:12px;" />
                        <span class="rd-unit">个 / 小时</span>
                    </div>
                    <div class="rd-tip">KEEP 分类与「保种」标签始终跳过；删除前会先汇报站点。</div>
                </div>

                <div class="rd-label">备注</div>
                <el-input v-model="form.description" type="textarea" :rows="2" placeholder="给自己看的说明，干跑时会一并显示" />

                <div class="rd-label">干跑</div>
                <div class="rd-row">
                    <el-button :loading="dryrunLoading" @click="runDryrun">拿当前种子跑一遍</el-button>
                    <span class="rd-unit" v-if="dryrun">检查 {{ dryrun.checked }} 个，命中 {{ dryrun.hit_count }} 个</span>
                </div>
                <div class="rd-dry" v-if="dryrun && dryrun.hits.length">
                    <div class="rd-dry-row" v-for="(h, i) in dryrun.hits.slice(0, 8)" :key="i">
                        <span class="rd-dry-name">{{ h.name }}</span>
                        <span class="rd-dry-meta">{{ h.site }} · {{ h.size_gb ? h.size_gb + ' GiB' : (h.seed_hours ? '做种 ' + h.seed_hours + 'h' : '') }}</span>
                    </div>
                </div>

                <div class="rd-label">启用</div>
                <el-switch v-model="form.enabled" active-text="保存后立即生效" />
            </div>
            <template #footer>
                <el-button @click="close">取消</el-button>
                <el-button type="primary" :loading="saving" @click="save">保存</el-button>
            </template>
        </el-drawer>
    `,
});
