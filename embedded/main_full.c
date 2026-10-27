#include <stdint.h>
#include <string.h>

#include "FreeRTOS.h"
#include "task.h"
#include "riscv-virt.h"

#define CHECKPOINT_MAGIC             UINT32_C(0x54434F53)
#define CHECKPOINT_SCHEMA_VERSION    UINT32_C(1)
#define CHECKPOINT_SENSOR_TASK_ID    UINT32_C(1)
#define CHECKPOINT_CONTROL_TASK_ID   UINT32_C(2)
#define CHECKPOINT_REQUEST           UINT32_C(1)
#define CHECKPOINT_ACK_FAILED        UINT32_C(1)
#define CHECKPOINT_ACK_READY         UINT32_C(2)
#define RECOVERY_PROGRESS_ACK        UINT32_C(4)
#define UART_RBR                     UINT32_C(0x10000000)
#define UART_LSR                     UINT32_C(0x10000005)
#define UART_LSR_DATA_READY          UINT8_C(0x01)
#define CONTROL_COMMAND_MAX          128U
#define RUN_ID_LENGTH                36U
#define UART_COMMAND_INJECT_STACK    UINT32_C(0x100)
#define STACK_PRESSURE_WORDS         144U
#define DEMO_TASK_STACK_DEPTH        (configMINIMAL_STACK_SIZE * 3U)
#define SENSOR_PERIOD_TICKS          UINT32_C(250)
#define CONTROL_PERIOD_TICKS         UINT32_C(1000)
#define SENSOR_WORK_ITERATIONS       UINT32_C(128)
#define CONTROL_WORK_ITERATIONS      UINT32_C(4096)

typedef struct
{
    const char *task_id;
    uint32_t checkpoint_task_id;
    TickType_t period_ticks;
    uint32_t work_iterations;
    uint32_t counter;
    uint32_t accumulator;
    BaseType_t restore_pending;
    volatile BaseType_t stack_pressure_pending;
} TaskContext;

typedef struct
{
    uint32_t magic;
    uint32_t schema_version;
    uint32_t task_id;
    uint32_t counter;
    uint32_t accumulator;
    uint32_t integrity_hash;
} EmbeddedCheckpoint;

typedef struct
{
    uint32_t counter;
    uint32_t accumulator;
} ProgressReport;

static TaskContext sensor_context = {
    "sensor-sampler", CHECKPOINT_SENSOR_TASK_ID,
    SENSOR_PERIOD_TICKS, SENSOR_WORK_ITERATIONS, 0U, 0U, pdFALSE, pdFALSE
};
static TaskContext control_context = {
    "control-loop", CHECKPOINT_CONTROL_TASK_ID,
    CONTROL_PERIOD_TICKS, CONTROL_WORK_ITERATIONS, 0U, 0U, pdFALSE, pdFALSE
};

static TaskHandle_t sensor_task_handle = NULL;
static TaskHandle_t control_task_handle = NULL;
static TaskHandle_t controller_task_handle = NULL;
static EmbeddedCheckpoint saved_checkpoint;
static ProgressReport recovery_progress;
static uint32_t recovery_progress_expected_task_id = 0U;
static char active_run_id[RUN_ID_LENGTH + 1U];
static char parsed_command_run_id[RUN_ID_LENGTH + 1U];

enum
{
    EVENT_NO_STATE = 0,
    EVENT_WITH_STATE = 1
};

static char *append_text(char *destination, const char *text)
{
    while (*text != '\0')
    {
        *destination++ = *text++;
    }
    *destination = '\0';
    return destination;
}

static char *append_u32(char *destination, uint32_t value)
{
    char digits[10];
    unsigned int digit_count = 0U;

    do
    {
        digits[digit_count++] = (char)('0' + (value % 10U));
        value /= 10U;
    } while (value != 0U);

    while (digit_count > 0U)
    {
        *destination++ = digits[--digit_count];
    }
    *destination = '\0';
    return destination;
}

static void copy_active_run_id(char destination[RUN_ID_LENGTH + 1U])
{
    size_t index;

    taskENTER_CRITICAL();
    for (index = 0U; index <= RUN_ID_LENGTH; index++)
    {
        destination[index] = active_run_id[index];
    }
    taskEXIT_CRITICAL();
}

static void set_active_run_id(const char source[RUN_ID_LENGTH + 1U])
{
    size_t index;

    taskENTER_CRITICAL();
    for (index = 0U; index <= RUN_ID_LENGTH; index++)
    {
        active_run_id[index] = source[index];
    }
    taskEXIT_CRITICAL();
}

static BaseType_t is_canonical_run_id(const char *value)
{
    size_t index;

    for (index = 0U; index < RUN_ID_LENGTH; index++)
    {
        char character = value[index];
        if (index == 8U || index == 13U || index == 18U || index == 23U)
        {
            if (character != '-')
            {
                return pdFALSE;
            }
        }
        else if (!((character >= '0' && character <= '9') ||
                   (character >= 'a' && character <= 'f')))
        {
            return pdFALSE;
        }
    }
    return value[RUN_ID_LENGTH] == '\0' ? pdTRUE : pdFALSE;
}

static BaseType_t parse_command_run_id(
    const char *command,
    size_t command_length,
    const char *prefix,
    char run_id[RUN_ID_LENGTH + 1U],
    BaseType_t allow_legacy)
{
    size_t prefix_length = 0U;
    size_t index;

    while (prefix[prefix_length] != '\0')
    {
        prefix_length++;
    }
    if (command_length < prefix_length)
    {
        return pdFALSE;
    }
    for (index = 0U; index < prefix_length; index++)
    {
        if (command[index] != prefix[index])
        {
            return pdFALSE;
        }
    }
    if (allow_legacy == pdTRUE && command_length == prefix_length)
    {
        run_id[0] = '\0';
        return pdTRUE;
    }
    if (command_length != prefix_length + RUN_ID_LENGTH + 1U ||
        command[prefix_length] != ' ')
    {
        return pdFALSE;
    }
    for (index = 0U; index < RUN_ID_LENGTH; index++)
    {
        run_id[index] = command[prefix_length + 1U + index];
    }
    run_id[RUN_ID_LENGTH] = '\0';
    if (is_canonical_run_id(run_id) != pdTRUE)
    {
        run_id[0] = '\0';
        return pdFALSE;
    }
    return pdTRUE;
}

static uint32_t hash_u32(uint32_t hash, uint32_t value)
{
    unsigned int byte_index;

    for (byte_index = 0U; byte_index < 4U; byte_index++)
    {
        hash ^= (uint8_t)(value & UINT32_C(0xff));
        hash *= UINT32_C(16777619);
        value >>= 8U;
    }
    return hash;
}

static uint32_t checkpoint_integrity(const EmbeddedCheckpoint *checkpoint)
{
    uint32_t hash = UINT32_C(2166136261);

    hash = hash_u32(hash, checkpoint->magic);
    hash = hash_u32(hash, checkpoint->schema_version);
    hash = hash_u32(hash, checkpoint->task_id);
    hash = hash_u32(hash, checkpoint->counter);
    hash = hash_u32(hash, checkpoint->accumulator);
    return hash;
}

static BaseType_t checkpoint_is_valid(const EmbeddedCheckpoint *checkpoint)
{
    return (checkpoint->magic == CHECKPOINT_MAGIC &&
            checkpoint->schema_version == CHECKPOINT_SCHEMA_VERSION &&
            (checkpoint->task_id == CHECKPOINT_SENSOR_TASK_ID ||
             checkpoint->task_id == CHECKPOINT_CONTROL_TASK_ID) &&
            checkpoint->counter < UINT32_MAX &&
            checkpoint->integrity_hash == checkpoint_integrity(checkpoint))
               ? pdTRUE
               : pdFALSE;
}

static void emit_task_event(const char *task_id,
                            const char *event,
                            int include_state,
                            uint32_t counter,
                            uint32_t accumulator,
                            uint32_t integrity_hash)
{
    char message[256];
    char *cursor = message;
    char run_id[RUN_ID_LENGTH + 1U];

    copy_active_run_id(run_id);

    if (include_state == EVENT_WITH_STATE)
    {
        cursor = append_text(cursor,
                             "{\"schema_version\":1,\"message_type\":\"task_event\","
                             "\"target\":\"freertos-qemu-rv32\",\"task_id\":\"");
        cursor = append_text(cursor, task_id);
        cursor = append_text(cursor, "\",\"event\":\"");
        cursor = append_text(cursor, event);
        cursor = append_text(cursor, "\",\"counter\":");
        cursor = append_u32(cursor, counter);
        cursor = append_text(cursor, ",\"accumulator\":");
        cursor = append_u32(cursor, accumulator);
        cursor = append_text(cursor, ",\"integrity_hash\":");
        cursor = append_u32(cursor, integrity_hash);
        if (strcmp(event, "checkpoint_saved") == 0)
        {
            cursor = append_text(cursor, ",\"checkpoint_size_bytes\":");
            cursor = append_u32(cursor, (uint32_t)sizeof(EmbeddedCheckpoint));
        }
    }
    else
    {
        cursor = append_text(cursor,
                             "{\"schema_version\":1,\"message_type\":\"task_event\","
                             "\"target\":\"freertos-qemu-rv32\",\"task_id\":\"");
        cursor = append_text(cursor, task_id);
        cursor = append_text(cursor, "\",\"event\":\"");
        cursor = append_text(cursor, event);
        cursor = append_text(cursor, "\"");
    }
    if (run_id[0] != '\0')
    {
        cursor = append_text(cursor, ",\"run_id\":\"");
        cursor = append_text(cursor, run_id);
        cursor = append_text(cursor, "\"");
    }
    (void)append_text(cursor, "}");
    vSendString(message);
}

static uint32_t advance_accumulator(uint32_t accumulator,
                                    uint32_t next_counter,
                                    uint32_t work_iterations)
{
    uint32_t iteration;

    for (iteration = 0U; iteration < work_iterations; iteration++)
    {
        accumulator = (accumulator * UINT32_C(33)) ^
                      (next_counter + iteration + UINT32_C(0x9e3779b9));
    }
    return accumulator;
}

static void emit_task_telemetry(const TaskContext *context)
{
    char message[256];
    char *cursor;
    char run_id[RUN_ID_LENGTH + 1U];
    UBaseType_t stack_high_water = uxTaskGetStackHighWaterMark(NULL);
    const char *task_state;

    switch (eTaskGetState(xTaskGetCurrentTaskHandle()))
    {
        case eRunning:
            task_state = "running";
            break;
        case eReady:
            task_state = "ready";
            break;
        case eBlocked:
            task_state = "blocked";
            break;
        case eSuspended:
            task_state = "suspended";
            break;
        case eDeleted:
            task_state = "deleted";
            break;
        default:
            task_state = "unknown";
            break;
    }

    cursor = append_text(message,
                         "{\"schema_version\":1,\"message_type\":\"task_telemetry\","
                         "\"target\":\"freertos-qemu-rv32\",\"task_id\":\"");
    copy_active_run_id(run_id);
    cursor = append_text(cursor, context->task_id);
    cursor = append_text(cursor, "\",\"task_state\":\"");
    cursor = append_text(cursor, task_state);
    cursor = append_text(cursor, "\",\"tick_count\":");
    cursor = append_u32(cursor, (uint32_t)xTaskGetTickCount());
    cursor = append_text(cursor, ",\"execution_count\":");
    cursor = append_u32(cursor, context->counter);
    cursor = append_text(cursor, ",\"stack_high_water_mark_words\":");
    cursor = append_u32(cursor, (uint32_t)stack_high_water);
    if (strcmp(context->task_id, "sensor-sampler") == 0 &&
        run_id[0] != '\0')
    {
        cursor = append_text(cursor, ",\"run_id\":\"");
        cursor = append_text(cursor, run_id);
        cursor = append_text(cursor, "\"");
    }
    (void)append_text(cursor, "}");
    vSendString(message);
}

static uint32_t poll_checkpoint_command(void)
{
    static char command[CONTROL_COMMAND_MAX];
    static size_t command_length = 0U;
    static BaseType_t discard_until_newline = pdFALSE;

    while ((*(volatile uint8_t *)UART_LSR & UART_LSR_DATA_READY) != 0U)
    {
        char value = (char)*(volatile uint8_t *)UART_RBR;

        if (discard_until_newline == pdTRUE)
        {
            if (value == '\n')
            {
                discard_until_newline = pdFALSE;
                command_length = 0U;
            }
            continue;
        }
        if (value == '\r')
        {
            continue;
        }
        if (value == '\n')
        {
            static const char sensor_command[] = "CHECKPOINT sensor-sampler";
            static const char inject_command[] =
                "INJECT_STACK_PRESSURE sensor-sampler";
            uint32_t command_code = 0U;

            if (parse_command_run_id(
                    command,
                    command_length,
                    sensor_command,
                    parsed_command_run_id,
                    pdTRUE) == pdTRUE)
            {
                command_code = CHECKPOINT_SENSOR_TASK_ID;
            }
            else if (parse_command_run_id(
                         command,
                         command_length,
                         inject_command,
                         parsed_command_run_id,
                         pdFALSE) == pdTRUE)
            {
                command_code = UART_COMMAND_INJECT_STACK |
                               CHECKPOINT_SENSOR_TASK_ID;
            }
            if (command_code == 0U)
            {
                parsed_command_run_id[0] = '\0';
            }
            command_length = 0U;
            return command_code;
        }
        if (command_length >= sizeof(command) - 1U)
        {
            command_length = 0U;
            discard_until_newline = pdTRUE;
            continue;
        }
        command[command_length++] = value;
    }
    return 0U;
}

static void __attribute__((noinline)) apply_bounded_stack_pressure(
    TaskContext *context)
{
    volatile uint32_t bounded_use[STACK_PRESSURE_WORDS];
    uint32_t index;

    for (index = 0U; index < STACK_PRESSURE_WORDS; index++)
    {
        bounded_use[index] = context->counter + index;
    }
    emit_task_telemetry(context);
    bounded_use[0] ^= context->counter;
}

static BaseType_t task_stack_pressure_safe_point(TaskContext *context)
{
    BaseType_t apply_pressure;

    taskENTER_CRITICAL();
    apply_pressure = context->stack_pressure_pending;
    context->stack_pressure_pending = pdFALSE;
    taskEXIT_CRITICAL();

    if (apply_pressure == pdTRUE)
    {
        emit_task_event(context->task_id, "fault_injected",
                        EVENT_NO_STATE, 0U, 0U, 0U);
        return pdTRUE;
    }
    return pdFALSE;
}

static void task_checkpoint_safe_point(TaskContext *context)
{
    uint32_t notification_value = 0U;

    if (xTaskNotifyWait(0U, UINT32_MAX, &notification_value, 0U) != pdTRUE ||
        (notification_value & CHECKPOINT_REQUEST) == 0U)
    {
        return;
    }
    {
        EmbeddedCheckpoint checkpoint = {
            CHECKPOINT_MAGIC,
            CHECKPOINT_SCHEMA_VERSION,
            context->checkpoint_task_id,
            context->counter,
            context->accumulator,
            0U
        };
        checkpoint.integrity_hash = checkpoint_integrity(&checkpoint);

        if (checkpoint_is_valid(&checkpoint) != pdTRUE)
        {
            emit_task_event(context->task_id, "checkpoint_failed",
                            EVENT_NO_STATE, 0U, 0U, 0U);
            (void)xTaskNotify(controller_task_handle,
                              CHECKPOINT_ACK_FAILED,
                              eSetBits);
            return;
        }

        taskENTER_CRITICAL();
        saved_checkpoint = checkpoint;
        taskEXIT_CRITICAL();

        emit_task_event(context->task_id,
                        "checkpoint_saved",
                        EVENT_WITH_STATE,
                        checkpoint.counter,
                        checkpoint.accumulator,
                        checkpoint.integrity_hash);
        (void)xTaskNotify(controller_task_handle, CHECKPOINT_ACK_READY, eSetBits);

        /* The task owns this snapshot and cannot mutate its state after acknowledging. */
        vTaskSuspend(NULL);
    }
}

static void periodic_task(void *argument)
{
    TaskContext *context = (TaskContext *)argument;
    TickType_t previous_wake = xTaskGetTickCount();
    BaseType_t apply_stack_pressure;

    if (context->restore_pending == pdTRUE)
    {
        context->restore_pending = pdFALSE;
        emit_task_event(context->task_id,
                        "restore_succeeded",
                        EVENT_WITH_STATE,
                        context->counter,
                        context->accumulator,
                        saved_checkpoint.integrity_hash);
    }

    for (;;)
    {
        task_checkpoint_safe_point(context);
        apply_stack_pressure = task_stack_pressure_safe_point(context);

        context->counter++;
        context->accumulator = advance_accumulator(context->accumulator,
                                                   context->counter,
                                                   context->work_iterations);
        if (apply_stack_pressure == pdTRUE)
        {
            apply_bounded_stack_pressure(context);
        }
        else
        {
            emit_task_telemetry(context);
        }

        if (controller_task_handle != NULL)
        {
            uint32_t notification_value = 0U;

            taskENTER_CRITICAL();
            if (recovery_progress_expected_task_id == context->checkpoint_task_id &&
                context->counter == saved_checkpoint.counter + 1U &&
                context->counter != 0U)
            {
                recovery_progress.counter = context->counter;
                recovery_progress.accumulator = context->accumulator;
                recovery_progress_expected_task_id = 0U;
                notification_value = RECOVERY_PROGRESS_ACK;
            }
            taskEXIT_CRITICAL();

            if (notification_value != 0U)
            {
                emit_task_event(context->task_id,
                                "task_progress",
                                EVENT_WITH_STATE,
                                context->counter,
                                context->accumulator,
                                saved_checkpoint.integrity_hash);
                (void)xTaskNotify(controller_task_handle,
                                  RECOVERY_PROGRESS_ACK,
                                  eSetBits);
            }
        }

        vTaskDelayUntil(&previous_wake, context->period_ticks);
    }
}

static TaskContext *context_for_task_id(uint32_t task_id)
{
    if (task_id == CHECKPOINT_SENSOR_TASK_ID)
    {
        return &sensor_context;
    }
    if (task_id == CHECKPOINT_CONTROL_TASK_ID)
    {
        return &control_context;
    }
    return NULL;
}

static TaskHandle_t *handle_for_task_id(uint32_t task_id)
{
    if (task_id == CHECKPOINT_SENSOR_TASK_ID)
    {
        return &sensor_task_handle;
    }
    if (task_id == CHECKPOINT_CONTROL_TASK_ID)
    {
        return &control_task_handle;
    }
    return NULL;
}

static void recover_task(uint32_t task_id)
{
    uint32_t notification_value = 0U;
    TaskContext *context = context_for_task_id(task_id);
    TaskHandle_t *task_handle = handle_for_task_id(task_id);
    TaskHandle_t old_task;
    TaskHandle_t replacement_task = NULL;
    EmbeddedCheckpoint checkpoint;
    ProgressReport progress;
    BaseType_t create_result;

    if (context == NULL || task_handle == NULL || *task_handle == NULL)
    {
        return;
    }
    (void)xTaskNotify(*task_handle, CHECKPOINT_REQUEST, eSetBits);

    if (xTaskNotifyWait(0U,
                        UINT32_MAX,
                        &notification_value,
                        pdMS_TO_TICKS(3000U)) != pdTRUE ||
        (notification_value & CHECKPOINT_ACK_READY) == 0U)
    {
        emit_task_event(context->task_id, "checkpoint_failed",
                        EVENT_NO_STATE, 0U, 0U, 0U);
        return;
    }

    old_task = *task_handle;
    vTaskSuspend(old_task);

    taskENTER_CRITICAL();
    checkpoint = saved_checkpoint;
    taskEXIT_CRITICAL();

    if (checkpoint_is_valid(&checkpoint) != pdTRUE ||
        checkpoint.task_id != task_id)
    {
        emit_task_event(context->task_id,
                        "restore_failed",
                        EVENT_WITH_STATE,
                        checkpoint.counter,
                        checkpoint.accumulator,
                        checkpoint.integrity_hash);
        vTaskResume(old_task);
        emit_task_event(context->task_id, "recovery_failed",
                        EVENT_NO_STATE, 0U, 0U, 0U);
        return;
    }

    emit_task_event(context->task_id,
                    "restore_started",
                    EVENT_WITH_STATE,
                    checkpoint.counter,
                    checkpoint.accumulator,
                    checkpoint.integrity_hash);
    context->counter = checkpoint.counter;
    context->accumulator = checkpoint.accumulator;
    context->restore_pending = pdTRUE;
    taskENTER_CRITICAL();
    recovery_progress_expected_task_id = task_id;
    taskEXIT_CRITICAL();

    emit_task_event(context->task_id,
                    "task_restart_started",
                    EVENT_WITH_STATE,
                    checkpoint.counter,
                    checkpoint.accumulator,
                    checkpoint.integrity_hash);

    create_result = xTaskCreate(periodic_task,
                                context->task_id,
                                DEMO_TASK_STACK_DEPTH,
                                context,
                                tskIDLE_PRIORITY + 1U,
                                &replacement_task);
    if (create_result != pdPASS)
    {
        context->restore_pending = pdFALSE;
        taskENTER_CRITICAL();
        recovery_progress_expected_task_id = 0U;
        taskEXIT_CRITICAL();
        vTaskResume(old_task);
        emit_task_event(context->task_id, "recovery_failed",
                        EVENT_NO_STATE, 0U, 0U, 0U);
        return;
    }

    *task_handle = replacement_task;
    vTaskDelete(old_task);

    emit_task_event(context->task_id,
                    "task_restarted",
                    EVENT_WITH_STATE,
                    checkpoint.counter,
                    checkpoint.accumulator,
                    checkpoint.integrity_hash);

    notification_value = 0U;
    if (xTaskNotifyWait(0U,
                        UINT32_MAX,
                        &notification_value,
                        pdMS_TO_TICKS(3000U)) != pdTRUE ||
        (notification_value & RECOVERY_PROGRESS_ACK) == 0U)
    {
        emit_task_event(context->task_id, "recovery_failed",
                        EVENT_NO_STATE, 0U, 0U, 0U);
        return;
    }

    taskENTER_CRITICAL();
    progress = recovery_progress;
    taskEXIT_CRITICAL();

    if (progress.counter != checkpoint.counter + 1U ||
        progress.accumulator != advance_accumulator(checkpoint.accumulator,
                                                    checkpoint.counter + 1U,
                                                context->work_iterations))
    {
        emit_task_event(context->task_id,
                        "recovery_failed",
                        EVENT_WITH_STATE,
                        progress.counter,
                        progress.accumulator,
                        0U);
        return;
    }

    vTaskDelay(pdMS_TO_TICKS(1200U));
    emit_task_event(context->task_id,
                    "recovery_completed",
                    EVENT_WITH_STATE,
                    progress.counter,
                    progress.accumulator,
                    checkpoint.integrity_hash);
}

static void recovery_controller(void *argument)
{
    (void)argument;
    for (;;)
    {
        uint32_t command;
        while ((command = poll_checkpoint_command()) == 0U)
        {
            vTaskDelay(pdMS_TO_TICKS(10U));
        }
        {
            uint32_t task_id = command & UINT32_C(0xff);
            TaskContext *context = context_for_task_id(task_id);
            if ((command & UART_COMMAND_INJECT_STACK) != 0U)
            {
                if (context != NULL && parsed_command_run_id[0] != '\0' &&
                    context->stack_pressure_pending == pdFALSE)
                {
                    set_active_run_id(parsed_command_run_id);
                    taskENTER_CRITICAL();
                    context->stack_pressure_pending = pdTRUE;
                    taskEXIT_CRITICAL();
                    emit_task_event(context->task_id,
                                    "fault_injection_requested",
                                    EVENT_NO_STATE, 0U, 0U, 0U);
                }
            }
            else if (context != NULL && task_id == CHECKPOINT_SENSOR_TASK_ID)
            {
                char active_id[RUN_ID_LENGTH + 1U];
                copy_active_run_id(active_id);
                if (active_id[0] == '\0' ||
                    strcmp(parsed_command_run_id, active_id) == 0)
                {
                    emit_task_event(context->task_id, "checkpoint_requested",
                                    EVENT_NO_STATE, 0U, 0U, 0U);
                    recover_task(task_id);
                }
            }
        }
    }
}

void main_full(void)
{
    (void)xTaskCreate(periodic_task,
                      "sensor-sampler",
                      DEMO_TASK_STACK_DEPTH,
                      &sensor_context,
                      tskIDLE_PRIORITY + 1U,
                      &sensor_task_handle);
    (void)xTaskCreate(periodic_task,
                      "control-loop",
                      DEMO_TASK_STACK_DEPTH,
                      &control_context,
                      tskIDLE_PRIORITY + 1U,
                      &control_task_handle);
    (void)xTaskCreate(recovery_controller,
                      "recovery-controller",
                      configMINIMAL_STACK_SIZE * 2U,
                      NULL,
                      tskIDLE_PRIORITY + 2U,
                      &controller_task_handle);

    vTaskStartScheduler();
    vSendString("{\"schema_version\":1,\"message_type\":\"startup_error\","
                "\"target\":\"freertos-qemu-rv32\","
                "\"reason\":\"scheduler_returned\"}");
    for (;;)
    {
    }
}

void vFullDemoTickHookFunction(void)
{
}
