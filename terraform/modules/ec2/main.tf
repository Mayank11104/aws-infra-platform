# modules/ec2/main.tf

resource "aws_key_pair" "this" {
  key_name   = "${var.environment}-key"
  public_key = var.public_key_path != "" ? file(var.public_key_path) : var.public_key
}

resource "aws_instance" "this" {
  ami                    = var.ami_id
  instance_type          = var.instance_type
  subnet_id              = var.subnet_id
  vpc_security_group_ids = [var.security_group_id]
  key_name               = aws_key_pair.this.key_name

  root_block_device {
    volume_size = var.root_volume_size
    volume_type = "gp3"
  }

  tags = {
    Name        = "${var.environment}-web"
    Environment = var.environment
    Role        = "web"
  }
}
