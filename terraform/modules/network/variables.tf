variable "project" { type = string }

variable "vpc_cidr" { type = string }

variable "allowed_cidrs" {
  type        = map(string)
  description = <<-EOT
    アプリに到達できる送信元IPのホワイトリスト。「名札 => CIDR」で書く。
    名札はSGルールのdescriptionに入るので、あとで「これ誰のIP？」にならないよう
    人や拠点が分かる名前を付ける。例:
      { office = "203.0.113.10/32", maruyama-home = "198.51.100.24/32" }
    空にすると誰も到達できない（アプリは動くが接続できない）状態になる。
  EOT

  validation {
    # 全開放だけは事故なので弾く。ここを開けたい場合は設計から見直すこと。
    condition     = !contains(values(var.allowed_cidrs), "0.0.0.0/0")
    error_message = "allowed_cidrs に 0.0.0.0/0 は指定できません（ホワイトリストの意味が無くなるため）。"
  }
}

variable "allow_direct_api" {
  type        = bool
  description = "FastAPI(8000)にも直接つなげるか。falseなら画面(3000)だけ開ける"
  default     = true
}

variable "frontend_port" {
  type    = number
  default = 3000
}

variable "api_port" {
  type    = number
  default = 8000
}
