# Interface & Reflection (接口与反射)

## Interface (接口)

Go 的接口是隐式实现的 (Duck Typing)。

### 底层结构

接口在 Runtime 中由两个结构体表示：

#### 1. `iface` (带方法的接口)

用于定义了方法的接口 (e.g. `io.Reader`).

```go
type iface struct {
    tab  *itab          // 包含类型信息和方法表
    data unsafe.Pointer // 指向具体数据的指针
}
```

- **itab**: 缓存了具体类型 (`_type`) 和接口类型 (`inter`) 的匹配信息，以及虚函数表 (`fun`)。
- **动态派发**: 调用方法时，通过 `tab->fun[index]` 找到具体函数的地址。

#### 2. `eface` (空接口)

用于 `interface{}`，不包含任何方法。

```go
type eface struct {
    _type *_type         // 具体类型信息
    data  unsafe.Pointer // 指向具体数据的指针
}
```

### 关键特性

- **nil interface**: 只有当 `type` 和 `data` 都为 `nil` 时，接口才等于 `nil`。
  - 陷阱: 将一个 `nil` 的具体指针赋值给接口，该接口 **不等于** `nil` (因为 type 字段不为空)。

## Reflection (反射)

反射允许程序在运行时检查变量的类型和值。核心包 `reflect`。

### 三大定律 (Laws of Reflection)

1. **Reflection goes from Interface value to Reflection object.**
   - 接口值 -> 反射对象。
   - `reflect.TypeOf(i)` 和 `reflect.ValueOf(i)`。

2. **Reflection goes from Reflection object to Interface value.**
   - 反射对象 -> 接口值。
   - `v.Interface()`。可以将反射对象转换回 `interface{}`，然后通过断言转回具体类型。

3. **To modify a reflection object, the value must be settable.**
   - 要修改反射对象，值必须是可设置的 (settability)。
   - 传递给 `ValueOf` 的必须是**指针**，然后调用 `Elem()` 获取指针指向的元素，才能修改。

   ```go
   var x float64 = 3.4
   v := reflect.ValueOf(x)
   // v.SetFloat(7.1) // Error: will panic, v is not settable

   p := reflect.ValueOf(&x) // 传指针
   v = p.Elem()             // 获取元素
   v.SetFloat(7.1)          // OK
   ```

### 性能影响

- 反射涉及大量的类型检查和内存分配。
- 相比直接调用，反射慢 1-2 个数量级。
- 尽量避免在热点代码路径中使用反射 (如 JSON 序列化库通常会使用 `unsafe` 或代码生成来优化)。
