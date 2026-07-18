variable "project" {
  type        = string
  default     = "rag-v2"
  description = "リソース名のprefix"
}

variable "vpc_cidr" {
  type    = string
  default = "10.0.0.0/16"
}
