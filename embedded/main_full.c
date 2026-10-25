#include <stdint.h>

#include "FreeRTOS.h"
#include "task.h"
#include "riscv-virt.h"

#define CHECKPOINT_MAGIC             UINT32_C(0x54434F53)
#define CHECKPOINT_SCHEMA_VERSION    UINT32_C(1)
#define CHECKPOINT_TASK_ID           UINT32_C(1)
#define CHECKPOINT_REQUEST           UINT32_C(1)
#define CHECKPOINT_ACK_FAILED        UINT32_C(1)
#define CHECKPOINT_ACK_READY         UINT32_C(2)
#define RECOVERY_PROGRESS_ACK        UINT32_C(4)
#define UART_RBR                     UINT32_C(0x10000000)
#define UART_LSR                     UINT32_C(0x10000005)
#define UART_LSR_DATA_READY          UINT8_C(0x01)
#define CONTROL_COMMAND_MAX          64U
#define SENSOR_PERIOD_TICKS          UINT32_C(250)
#define CONTROL_PERIOD_TICKS         UINT32_C(1000)
#define SENSOR_WORK_ITERATIONS       UINT32_C(128)
#define CONTROL_WORK_ITERATIONS      UINT32_C(4096)

typedef struct
{
    const char *task_id;
    TickType_t period_ticks;
    uint32_t work_iterations;
    uint32_t counter;
    uint32_t accumulator;
    BaseType_t restore_pending;
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
    "sensor-sampler", SENSOR_PERIOD_TICKS, SENSOR_WORK_ITERATIONS, 0U, 0U, pdFALSE
};
static TaskContext control_context = {
    "control-loop", CONTROL_PERIOD_TICKS, CONTROL_WORK_ITERATIONS, 0U, 0U, pdFALSE
};

static TaskHandle_t sensor_task_handle = NULL;
static TaskHandle_t controller_task_handle = NULL;
static EmbeddedCheckpoint saved_checkpoint;
static ProgressReport recovery_progress;
static BaseType_t recovery_progress_expected = pdFALSE;

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
            checkpoint->task_id == CHECKPOINT_TASK_ID &&
            checkpoint->counter < UINT32_MAX &&
            checkpoint->integrity_hash == checkpoint_integrity(checkpoint))
               ? pdTRUE
               : pdFALSE;
}

static void emit_task_event(const char *event,
                            int include_state,
                            uint32_t counter,
                            uint32_t accumulator,
                            uint32_t integrity_hash)
{
    char message[256];
    char *cursor = message;

    if (include_state == EVENT_WITH_STATE)
    {
        cursor = append_text(cursor,
                             "{\"schema_version\":1,\"message_type\":\"task_event\","
                             "\"target\":\"freertos-qemu-rv32\","
                             "\"task_id\":\"sensor-sampler\",\"event\":\"");
        cursor = append_text(cursor, event);
        cursor = append_text(cursor, "\",\"counter\":");
        cursor = append_u32(cursor, counter);
        cursor = append_text(cursor, ",\"accumulator\":");
        cursor = append_u32(cursor, accumulator);
        cursor = append_text(cursor, ",\"integrity_hash\":");
        cursor = append_u32(cursor, integrity_hash);
    }
    else
    {
        cursor = append_text(cursor,
                             "{\"schema_version\":1,\"message_type\":\"task_event\","
                             "\"target\":\"freertos-qemu-rv32\","
                             "\"task_id\":\"sensor-sampler\",\"event\":\"");
        cursor = append_text(cursor, event);
    }
    (void)append_text(cursor, include_state == EVENT_WITH_STATE ? "}" : "\"}");
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
    cursor = append_text(cursor, context->task_id);
    cursor = append_text(cursor, "\",\"task_state\":\"");
    cursor = append_text(cursor, task_state);
    cursor = append_text(cursor, "\",\"tick_count\":");
    cursor = append_u32(cursor, (uint32_t)xTaskGetTickCount());
    cursor = append_text(cursor, ",\"execution_count\":");
    cursor = append_u32(cursor, context->counter);
    cursor = append_text(cursor, ",\"stack_high_water_mark_words\":");
    cursor = append_u32(cursor, (uint32_t)stack_high_water);
    (void)append_text(cursor, "}");
    vSendString(message);
}

static BaseType_t poll_checkpoint_command(void)
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
            static const char expected[] = "CHECKPOINT sensor-sampler";
            BaseType_t matched = pdFALSE;

            if (command_length == sizeof(expected) - 1U)
            {
                size_t index;
                matched = pdTRUE;
                for (index = 0U; index < sizeof(expected) - 1U; index++)
                {
                    if (command[index] != expected[index])
                    {
                        matched = pdFALSE;
                        break;
                    }
                }
            }
            command_length = 0U;
            return matched;
        }
        if (command_length >= sizeof(command) - 1U)
        {
            command_length = 0U;
            discard_until_newline = pdTRUE;
            continue;
        }
        command[command_length++] = value;
    }
    return pdFALSE;
}

static void task_checkpoint_safe_point(TaskContext *context)
{
    uint32_t notification_value = 0U;

    if (context != &sensor_context ||
        xTaskNotifyWait(0U, UINT32_MAX, &notification_value, 0U) != pdTRUE ||
        (notification_value & CHECKPOINT_REQUEST) == 0U)
    {
        return;
    }
    {
        EmbeddedCheckpoint checkpoint = {
            CHECKPOINT_MAGIC,
            CHECKPOINT_SCHEMA_VERSION,
            CHECKPOINT_TASK_ID,
            context->counter,
            context->accumulator,
            0U
        };
        checkpoint.integrity_hash = checkpoint_integrity(&checkpoint);

        if (checkpoint_is_valid(&checkpoint) != pdTRUE)
        {
            emit_task_event("checkpoint_failed", EVENT_NO_STATE, 0U, 0U, 0U);
            (void)xTaskNotify(controller_task_handle,
                              CHECKPOINT_ACK_FAILED,
                              eSetBits);
            return;
        }

        taskENTER_CRITICAL();
        saved_checkpoint = checkpoint;
        taskEXIT_CRITICAL();

        emit_task_event("checkpoint_saved",
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

    if (context->restore_pending == pdTRUE)
    {
        context->restore_pending = pdFALSE;
        emit_task_event("restore_succeeded",
                        EVENT_WITH_STATE,
                        context->counter,
                        context->accumulator,
                        saved_checkpoint.integrity_hash);
    }

    for (;;)
    {
        task_checkpoint_safe_point(context);

        context->counter++;
        context->accumulator = advance_accumulator(context->accumulator,
                                                   context->counter,
                                                   context->work_iterations);
        emit_task_telemetry(context);

        if (context == &sensor_context && controller_task_handle != NULL)
        {
            uint32_t notification_value = 0U;

            taskENTER_CRITICAL();
            if (recovery_progress_expected == pdTRUE &&
                context->counter == saved_checkpoint.counter + 1U &&
                context->counter != 0U)
            {
                recovery_progress.counter = context->counter;
                recovery_progress.accumulator = context->accumulator;
                recovery_progress_expected = pdFALSE;
                notification_value = RECOVERY_PROGRESS_ACK;
            }
            taskEXIT_CRITICAL();

            if (notification_value != 0U)
            {
                emit_task_event("task_progress",
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

static void recover_sensor_task(void)
{
    uint32_t notification_value = 0U;
    TaskHandle_t old_sensor_task;
    TaskHandle_t replacement_task = NULL;
    EmbeddedCheckpoint checkpoint;
    ProgressReport progress;
    BaseType_t create_result;

    (void)xTaskNotify(sensor_task_handle, CHECKPOINT_REQUEST, eSetBits);

    if (xTaskNotifyWait(0U,
                        UINT32_MAX,
                        &notification_value,
                        pdMS_TO_TICKS(3000U)) != pdTRUE ||
        (notification_value & CHECKPOINT_ACK_READY) == 0U)
    {
        emit_task_event("checkpoint_failed", EVENT_NO_STATE, 0U, 0U, 0U);
        return;
    }

    old_sensor_task = sensor_task_handle;
    vTaskSuspend(old_sensor_task);

    taskENTER_CRITICAL();
    checkpoint = saved_checkpoint;
    taskEXIT_CRITICAL();

    if (checkpoint_is_valid(&checkpoint) != pdTRUE)
    {
        emit_task_event("restore_failed",
                        EVENT_WITH_STATE,
                        checkpoint.counter,
                        checkpoint.accumulator,
                        checkpoint.integrity_hash);
        vTaskResume(old_sensor_task);
        emit_task_event("recovery_failed", EVENT_NO_STATE, 0U, 0U, 0U);
        return;
    }

    sensor_context.counter = checkpoint.counter;
    sensor_context.accumulator = checkpoint.accumulator;
    sensor_context.restore_pending = pdTRUE;
    taskENTER_CRITICAL();
    recovery_progress_expected = pdTRUE;
    taskEXIT_CRITICAL();

    emit_task_event("task_restart_started",
                    EVENT_WITH_STATE,
                    checkpoint.counter,
                    checkpoint.accumulator,
                    checkpoint.integrity_hash);

    create_result = xTaskCreate(periodic_task,
                                "sensor-sampler",
                                configMINIMAL_STACK_SIZE * 2U,
                                &sensor_context,
                                tskIDLE_PRIORITY + 1U,
                                &replacement_task);
    if (create_result != pdPASS)
    {
        sensor_context.restore_pending = pdFALSE;
        taskENTER_CRITICAL();
        recovery_progress_expected = pdFALSE;
        taskEXIT_CRITICAL();
        vTaskResume(old_sensor_task);
        emit_task_event("recovery_failed", EVENT_NO_STATE, 0U, 0U, 0U);
        return;
    }

    sensor_task_handle = replacement_task;
    vTaskDelete(old_sensor_task);

    emit_task_event("task_restarted",
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
        emit_task_event("recovery_failed", EVENT_NO_STATE, 0U, 0U, 0U);
        return;
    }

    taskENTER_CRITICAL();
    progress = recovery_progress;
    taskEXIT_CRITICAL();

    if (progress.counter != checkpoint.counter + 1U ||
        progress.accumulator != advance_accumulator(checkpoint.accumulator,
                                                    checkpoint.counter + 1U,
                                                    SENSOR_WORK_ITERATIONS))
    {
        emit_task_event("recovery_failed",
                        EVENT_WITH_STATE,
                        progress.counter,
                        progress.accumulator,
                        0U);
        return;
    }

    /* Allow the independent control-loop task to report progress during recovery. */
    vTaskDelay(pdMS_TO_TICKS(1200U));
    emit_task_event("recovery_completed",
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
        while (poll_checkpoint_command() != pdTRUE)
        {
            vTaskDelay(pdMS_TO_TICKS(10U));
        }
        emit_task_event("checkpoint_requested", EVENT_NO_STATE, 0U, 0U, 0U);
        recover_sensor_task();
    }
}

void main_full(void)
{
    (void)xTaskCreate(periodic_task,
                      "sensor-sampler",
                      configMINIMAL_STACK_SIZE * 2U,
                      &sensor_context,
                      tskIDLE_PRIORITY + 1U,
                      &sensor_task_handle);
    (void)xTaskCreate(periodic_task,
                      "control-loop",
                      configMINIMAL_STACK_SIZE * 2U,
                      &control_context,
                      tskIDLE_PRIORITY + 1U,
                      NULL);
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
