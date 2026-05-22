/*
  Copyright (c) 2009-2017 Dave Gamble and cJSON contributors
  MIT License - https://github.com/DaveGamble/cJSON
  Minimal implementation for dot_updater use.
*/

#include <string.h>
#include <stdio.h>
#include <math.h>
#include <stdlib.h>
#include <limits.h>
#include <ctype.h>
#include "cJSON.h"

static cJSON *cJSON_New_Item(void) {
    cJSON *node = (cJSON*)malloc(sizeof(cJSON));
    if (node) memset(node, 0, sizeof(cJSON));
    return node;
}

void cJSON_Delete(cJSON *item) {
    cJSON *next;
    while (item) {
        next = item->next;
        if (item->child) cJSON_Delete(item->child);
        if (item->valuestring) free(item->valuestring);
        if (item->string) free(item->string);
        free(item);
        item = next;
    }
}

static const char *skip(const char *in) {
    while (in && *in && (unsigned char)*in <= 32) in++;
    return in;
}

static const char *parse_string(cJSON *item, const char *str) {
    const char *ptr = str + 1;
    char *out;
    int len = 0;
    if (*str != '\"') return NULL;
    while (*ptr != '\"' && *ptr && ++len) {
        if (*ptr++ == '\\') ptr++;
    }
    out = (char*)malloc(len + 1);
    if (!out) return NULL;
    ptr = str + 1;
    char *ptr2 = out;
    while (*ptr != '\"' && *ptr) {
        if (*ptr != '\\') *ptr2++ = *ptr++;
        else {
            ptr++;
            switch (*ptr) {
                case 'n': *ptr2++ = '\n'; break;
                case 't': *ptr2++ = '\t'; break;
                case 'r': *ptr2++ = '\r'; break;
                default:  *ptr2++ = *ptr; break;
            }
            ptr++;
        }
    }
    *ptr2 = 0;
    item->valuestring = out;
    item->type = cJSON_String;
    return ptr + 1;
}

static const char *parse_number(cJSON *item, const char *num) {
    double n = 0, sign = 1;
    if (*num == '-') { sign = -1; num++; }
    while (*num >= '0' && *num <= '9') n = n * 10 + (*num++ - '0');
    item->valuedouble = sign * n;
    item->valueint = (int)(sign * n);
    item->type = cJSON_Number;
    return num;
}

static const char *parse_value(cJSON *item, const char *value);

static const char *parse_array(cJSON *item, const char *value) {
    cJSON *child;
    if (*value != '[') return NULL;
    item->type = cJSON_Array;
    value = skip(value + 1);
    if (*value == ']') return value + 1;
    item->child = child = cJSON_New_Item();
    if (!item->child) return NULL;
    value = skip(parse_value(child, skip(value)));
    if (!value) return NULL;
    while (*value == ',') {
        cJSON *new_item = cJSON_New_Item();
        if (!new_item) return NULL;
        child->next = new_item;
        new_item->prev = child;
        child = new_item;
        value = skip(parse_value(child, skip(value + 1)));
        if (!value) return NULL;
    }
    if (*value == ']') return value + 1;
    return NULL;
}

static const char *parse_object(cJSON *item, const char *value) {
    cJSON *child;
    if (*value != '{') return NULL;
    item->type = cJSON_Object;
    value = skip(value + 1);
    if (*value == '}') return value + 1;
    item->child = child = cJSON_New_Item();
    if (!item->child) return NULL;
    value = skip(parse_string(child, skip(value)));
    if (!value) return NULL;
    child->string = child->valuestring;
    child->valuestring = NULL;
    if (*value != ':') return NULL;
    value = skip(parse_value(child, skip(value + 1)));
    if (!value) return NULL;
    while (*value == ',') {
        cJSON *new_item = cJSON_New_Item();
        if (!new_item) return NULL;
        child->next = new_item;
        new_item->prev = child;
        child = new_item;
        value = skip(parse_string(child, skip(value + 1)));
        if (!value) return NULL;
        child->string = child->valuestring;
        child->valuestring = NULL;
        if (*value != ':') return NULL;
        value = skip(parse_value(child, skip(value + 1)));
        if (!value) return NULL;
    }
    if (*value == '}') return value + 1;
    return NULL;
}

static const char *parse_value(cJSON *item, const char *value) {
    if (!value) return NULL;
    if (!strncmp(value, "null",  4)) { item->type = cJSON_NULL;  return value + 4; }
    if (!strncmp(value, "false", 5)) { item->type = cJSON_False; return value + 5; }
    if (!strncmp(value, "true",  4)) { item->type = cJSON_True;  return value + 4; }
    if (*value == '\"') return parse_string(item, value);
    if (*value == '-' || (*value >= '0' && *value <= '9')) return parse_number(item, value);
    if (*value == '[') return parse_array(item, value);
    if (*value == '{') return parse_object(item, value);
    return NULL;
}

cJSON *cJSON_Parse(const char *value) {
    cJSON *c = cJSON_New_Item();
    if (!c) return NULL;
    if (!parse_value(c, skip(value))) {
        cJSON_Delete(c);
        return NULL;
    }
    return c;
}

cJSON *cJSON_GetObjectItem(const cJSON *object, const char *string) {
    cJSON *c = object ? object->child : NULL;
    while (c && strcmp(c->string, string)) c = c->next;
    return c;
}

cJSON *cJSON_GetArrayItem(cJSON *array, int index) {
    cJSON *c = array ? array->child : NULL;
    while (c && index > 0) { index--; c = c->next; }
    return c;
}

int cJSON_GetArraySize(cJSON *array) {
    cJSON *c = array ? array->child : NULL;
    int i = 0;
    while (c) { i++; c = c->next; }
    return i;
}

int cJSON_IsArray(const cJSON *item)  { return item && (item->type == cJSON_Array); }
int cJSON_IsObject(const cJSON *item) { return item && (item->type == cJSON_Object); }
int cJSON_IsString(const cJSON *item) { return item && (item->type == cJSON_String); }
int cJSON_IsNumber(const cJSON *item) { return item && (item->type == cJSON_Number); }
