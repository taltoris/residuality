/*
  Copyright (c) 2009-2017 Dave Gamble and cJSON contributors
  MIT License - https://github.com/DaveGamble/cJSON
  Version: 1.7.18 (minimal subset for dot_updater)
*/

#ifndef cJSON__h
#define cJSON__h

#ifdef __cplusplus
extern "C" {
#endif

#include <stddef.h>

#define cJSON_Invalid (0)
#define cJSON_False   (1 << 0)
#define cJSON_True    (1 << 1)
#define cJSON_NULL    (1 << 2)
#define cJSON_Number  (1 << 3)
#define cJSON_String  (1 << 4)
#define cJSON_Array   (1 << 5)
#define cJSON_Object  (1 << 6)
#define cJSON_Raw     (1 << 7)

typedef struct cJSON {
    struct cJSON *next;
    struct cJSON *prev;
    struct cJSON *child;
    int type;
    char *valuestring;
    int valueint;
    double valuedouble;
    char *string;
} cJSON;

extern cJSON *cJSON_Parse(const char *value);
extern void   cJSON_Delete(cJSON *item);
extern cJSON *cJSON_GetObjectItem(const cJSON *object, const char *string);
extern cJSON *cJSON_GetArrayItem(cJSON *array, int index);
extern int    cJSON_GetArraySize(cJSON *array);
extern int    cJSON_IsArray(const cJSON *item);
extern int    cJSON_IsObject(const cJSON *item);
extern int    cJSON_IsString(const cJSON *item);
extern int    cJSON_IsNumber(const cJSON *item);

#ifdef __cplusplus
}
#endif

#endif
